# Copyright 2021 AI Redefined Inc. <dev+cogment@ai-r.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from data_pb2 import (
    ActorConfig,
    ActorParams,
    EnvironmentConfig,
    EnvironmentParams,
    MLPNetworkConfig,
    ############ TUTORIAL STEP 4 ############
    SimpleBCTrainingConfig,
    ##########################################
    SimpleBCTrainingRunConfig,
    TrialConfig,
)

from cogment_verse_torch_agents.utils.tensors import tensor_from_cog_obs, tensor_from_cog_action, cog_action_from_tensor

from cogment_verse import AgentAdapter, MlflowExperimentTracker

from cogment.api.common_pb2 import TrialState
import cogment

import asyncio
import logging
import torch

############ TUTORIAL STEP 4 ############
import numpy as np

##########################################
import copy

from collections import namedtuple


SimpleBCModel = namedtuple("SimpleBCModel", ["model_id", "version_number", "policy_network"])

log = logging.getLogger(__name__)

# pylint: disable=arguments-differ
class SimpleBCAgentAdapterTutorialStep4(AgentAdapter):
    def __init__(self):
        super().__init__()
        self._dtype = torch.float

    @staticmethod
    async def run_async(func, *args):
        """Run a given function asynchronously in the default thread pool"""
        event_loop = asyncio.get_running_loop()
        return await event_loop.run_in_executor(None, func, *args)

    def _create(
        self,
        model_id,
        observation_size,
        action_count,
        policy_network_hidden_size=64,
        **kwargs,
    ):
        return SimpleBCModel(
            model_id=model_id,
            version_number=1,
            policy_network=torch.nn.Sequential(
                torch.nn.Linear(observation_size, policy_network_hidden_size),
                torch.nn.BatchNorm1d(policy_network_hidden_size),
                torch.nn.ReLU(),
                torch.nn.Linear(policy_network_hidden_size, policy_network_hidden_size),
                torch.nn.BatchNorm1d(policy_network_hidden_size),
                torch.nn.ReLU(),
                torch.nn.Linear(policy_network_hidden_size, action_count),
            ).to(self._dtype),
        )

    def _load(self, model_id, version_number, version_user_data, model_data_f):
        policy_network = torch.load(model_data_f)
        assert isinstance(policy_network, torch.nn.Sequential)
        return SimpleBCModel(model_id=model_id, version_number=version_number, policy_network=policy_network)

    def _save(self, model, model_data_f):
        assert isinstance(model, SimpleBCModel)
        torch.save(model.policy_network, model_data_f)
        return {}

    def _create_actor_implementations(self):
        async def impl(actor_session):
            actor_session.start()

            config = actor_session.config

            model, version_info = await self.retrieve_version(config.model_id, config.model_version)
            model_version_number = version_info["version_number"]
            log.info(f"Starting trial with model v{model_version_number}")

            # Retrieve the policy network and set it to "eval" mode
            policy_network = copy.deepcopy(model.policy_network)
            policy_network.eval()

            @torch.no_grad()
            def compute_action(event):
                obs = tensor_from_cog_obs(event.observation.snapshot, dtype=self._dtype)
                scores = policy_network(obs.view(1, -1))
                probs = torch.softmax(scores, dim=-1)
                action = torch.distributions.Categorical(probs).sample()
                return action

            async for event in actor_session.event_loop():
                if event.observation and event.type == cogment.EventType.ACTIVE:
                    action = await self.run_async(compute_action, event)
                    actor_session.do_action(cog_action_from_tensor(action))

        return {
            "simple_bc": (impl, ["agent"]),
        }

    def _create_run_implementations(self):
        async def sample_producer_impl(run_sample_producer_session):
            assert run_sample_producer_session.count_actors() == 2

            async for sample in run_sample_producer_session.get_all_samples():
                if sample.get_trial_state() == TrialState.ENDED:
                    break

                observation = tensor_from_cog_obs(sample.get_actor_observation(0), dtype=self._dtype)

                agent_action = sample.get_actor_action(0)
                teacher_action = sample.get_actor_action(1)

                # Check for teacher override.
                # Teacher action -1 corresponds to teacher approval,
                # i.e. the teacher considers the action taken by the agent to be correct
                if teacher_action.discrete_action != -1:
                    action = tensor_from_cog_action(teacher_action)
                    run_sample_producer_session.produce_training_sample((True, observation, action))
                else:
                    action = tensor_from_cog_action(agent_action)
                    run_sample_producer_session.produce_training_sample((False, observation, action))

        async def run_impl(run_session):
            xp_tracker = MlflowExperimentTracker(run_session.params_name, run_session.run_id)

            config = run_session.config
            assert config.environment.config.player_count == 1

            xp_tracker.log_params(
                config.training,
                config.environment.config,
                environment=config.environment.implementation,
                policy_network_hidden_size=config.policy_network.hidden_size,
            )

            model_id = f"{run_session.run_id}_model"

            # Initializing a model
            model, _version_info = await self.create_and_publish_initial_version(
                model_id,
                observation_size=config.actor.num_input,
                action_count=config.actor.num_action,
                policy_network_hidden_size=config.policy_network.hidden_size,
            )

            # Helper function to create a trial configuration
            def create_trial_config(trial_idx):
                env_params = copy.deepcopy(config.environment)
                env_params.config.seed = env_params.config.seed + trial_idx
                agent_actor_params = ActorParams(
                    name="agent_1",
                    actor_class="agent",
                    implementation="simple_bc",
                    config=ActorConfig(
                        model_id=model_id,
                        model_version=-1,
                        num_input=config.actor.num_input,
                        num_action=config.actor.num_action,
                        environment_implementation=config.environment.implementation,
                    ),
                )

                teacher_actor_params = ActorParams(
                    name="web_actor",
                    actor_class="teacher_agent",
                    implementation="client",
                    config=ActorConfig(
                        num_input=config.actor.num_input,
                        num_action=config.actor.num_action,
                        environment_implementation=config.environment.implementation,
                    ),
                )

                return TrialConfig(
                    run_id=run_session.run_id,
                    environment=env_params,
                    actors=[agent_actor_params, teacher_actor_params],
                )

            ############ TUTORIAL STEP 4 ############
            # Configure the optimizer
            optimizer = torch.optim.Adam(
                model.policy_network.parameters(),
                lr=config.training.learning_rate,
            )

            # Keep accumulated observations/actions around
            observations = []
            actions = []

            loss_fn = torch.nn.CrossEntropyLoss()

            def train_step():
                # Sample a batch of observations/actions
                batch_indices = np.random.default_rng().integers(0, len(observations), config.training.batch_size)
                batch_obs = torch.vstack([observations[i] for i in batch_indices])
                batch_act = torch.vstack([actions[i] for i in batch_indices]).view(-1)

                model.policy_network.train()
                pred_policy = model.policy_network(batch_obs)
                loss = loss_fn(pred_policy, batch_act)

                # Backprop!
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                return loss.item()

            ##########################################

            # Rollout a bunch of trials
            async for (
                ############ TUTORIAL STEP 4 ############
                step_idx,
                step_timestamp,
                ##########################################
                _trial_id,
                _tick_id,
                sample,
            ) in run_session.start_trials_and_wait_for_termination(
                trial_configs=[create_trial_config(trial_idx) for trial_idx in range(config.training.trial_count)],
                max_parallel_trials=config.training.max_parallel_trials,
            ):
                ############ TUTORIAL STEP 4 ############
                (_demonstration, observation, action) = sample
                # Can be uncommented to only use samples coming from the teacher
                # (demonstration, observation, action) = sample
                # if not demonstration:
                #     continue
                observations.append(observation)
                actions.append(action)

                if len(observations) < config.training.batch_size:
                    continue

                loss = await self.run_async(train_step)

                # Publish the newly trained version every 100 steps
                if step_idx % 100 == 0:
                    version_info = await self.publish_version(model_id, model)

                    xp_tracker.log_metrics(
                        step_timestamp,
                        step_idx,
                        model_version_number=version_info["version_number"],
                        loss=loss,
                        total_samples=len(observations),
                    )
                ##########################################

        return {
            "simple_bc_training": (
                sample_producer_impl,
                run_impl,
                SimpleBCTrainingRunConfig(
                    environment=EnvironmentParams(
                        implementation="gym/LunarLander-v2",
                        config=EnvironmentConfig(seed=12, player_count=1, framestack=1, render=True, render_width=256),
                    ),
                    ############ TUTORIAL STEP 4 ############
                    training=SimpleBCTrainingConfig(
                        trial_count=100,
                        max_parallel_trials=1,
                        discount_factor=0.95,
                        learning_rate=0.01,
                    ),
                    ##########################################
                    policy_network=MLPNetworkConfig(hidden_size=64),
                ),
            )
        }