"""Multi-agent system: coordinates 10 independent DQN agents."""

import logging
from pathlib import Path

import numpy as np

from dadqn_v3.config import SCALABLE_SERVICES, STEPS_PER_EPISODE, DQN_CONFIG, SLA_LATENCY_MS
from dadqn_v3.agents.dqn_agent import ServiceDQNAgent
from dadqn_v3.environments.service_agent_env import ServiceAgentEnv

logger = logging.getLogger(__name__)


class MultiAgentSystem:

    def __init__(self, agent_type="dqn", config=None):
        self.agent_type = agent_type
        cfg = config or DQN_CONFIG
        self.agents = {svc: ServiceDQNAgent(svc, cfg) for svc in SCALABLE_SERVICES}
        self.agent_envs = {}

    def init_envs(self, shared_state):
        self.agent_envs = {svc: ServiceAgentEnv(svc, shared_state) for svc in SCALABLE_SERVICES}

    def collect_actions(self, deterministic=False):
        return {svc: agent.select_action(self.agent_envs[svc].build_observation(), deterministic=deterministic)
                for svc, agent in self.agents.items()}

    def train_episode(self, env):
        shared_state = env.reset()
        self.init_envs(shared_state)
        ep_rewards = {svc: 0.0 for svc in SCALABLE_SERVICES}
        ep_latencies, ep_pods = [], []

        max_steps = getattr(env, "max_steps", STEPS_PER_EPISODE)
        for step in range(max_steps):
            observations = {svc: self.agent_envs[svc].build_observation() for svc in SCALABLE_SERVICES}
            actions = self.collect_actions(deterministic=False)
            shared_state, rewards, done, info = env.step(actions)

            for svc in SCALABLE_SERVICES:
                next_obs = self.agent_envs[svc].build_observation()
                self.agents[svc].store_transition(observations[svc], actions[svc], rewards[svc], next_obs, done)
                ep_rewards[svc] += rewards[svc]

            ep_latencies.append(info["frontend_latency_ms"])
            ep_pods.append(info["total_pods"])
            if done:
                break

        return {
            "total_reward": sum(ep_rewards.values()) / len(SCALABLE_SERVICES),
            "avg_latency_ms": np.mean(ep_latencies) if ep_latencies else 0,
            "avg_pods": np.mean(ep_pods) if ep_pods else 10,
            "sla_violation_rate": sum(1 for l in ep_latencies if l > SLA_LATENCY_MS) / max(len(ep_latencies), 1),
        }

    def evaluate_episode(self, env, max_steps=None):
        steps = max_steps or getattr(env, "max_steps", STEPS_PER_EPISODE)
        shared_state = env.reset()
        self.init_envs(shared_state)

        lats, pods_list = [], []
        total_r = 0.0

        for step in range(steps):
            actions = self.collect_actions(deterministic=True)
            shared_state, rewards, done, info = env.step(actions)
            total_r += sum(rewards.values()) / len(SCALABLE_SERVICES)
            lats.append(info["frontend_latency_ms"])
            pods_list.append(info["total_pods"])
            if done:
                break

        return {
            "total_reward": total_r,
            "avg_latency_ms": np.mean(lats) if lats else 0,
            "avg_pods": np.mean(pods_list) if pods_list else 10,
            "sla_violation_rate": sum(1 for l in lats if l > SLA_LATENCY_MS) / max(len(lats), 1),
        }

    def save_all(self, directory):
        for svc, agent in self.agents.items():
            agent.save(directory)

    def load_all(self, directory):
        for svc, agent in self.agents.items():
            agent.load(directory)
