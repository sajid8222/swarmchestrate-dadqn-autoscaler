"""Per-service DQN agent (baseline comparison)."""

import logging
from pathlib import Path

import numpy as np
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import DQN

from dadqn_v3.config import DQN_CONFIG, NUM_ACTIONS
from dadqn_v3.service_graph import get_observation_dim

logger = logging.getLogger(__name__)


class _DummyEnv(gym.Env):
    def __init__(self, obs_dim):
        super().__init__()
        self.observation_space = spaces.Box(0.0, 1.0, shape=(obs_dim,), dtype=np.float32)
        self.action_space = spaces.Discrete(NUM_ACTIONS)
    def reset(self, **kw):
        return np.zeros(self.observation_space.shape, dtype=np.float32), {}
    def step(self, a):
        return np.zeros(self.observation_space.shape, dtype=np.float32), 0.0, False, False, {}


class ServiceDQNAgent:

    def __init__(self, service_name: str, config: dict = None):
        self.service_name = service_name
        self.obs_dim = get_observation_dim(service_name)
        self.config = config or DQN_CONFIG
        self.model = DQN("MlpPolicy", _DummyEnv(self.obs_dim),
                         learning_rate=self.config["learning_rate"],
                         buffer_size=self.config["buffer_size"],
                         learning_starts=self.config["learning_starts"],
                         batch_size=self.config["batch_size"],
                         gamma=self.config["gamma"],
                         target_update_interval=self.config["target_update_interval"],
                         exploration_fraction=self.config["exploration_fraction"],
                         exploration_final_eps=self.config["exploration_final_eps"],
                         policy_kwargs=self.config["policy_kwargs"],
                         verbose=0)
        # Initialize logger so model.train() works
        from stable_baselines3.common.utils import configure_logger
        self.model.set_logger(configure_logger(verbose=0))
        self.model.num_timesteps = 0
        self.model._setup_learn(total_timesteps=100000, reset_num_timesteps=False)
        self.total_steps = 0

    def select_action(self, obs: np.ndarray, deterministic: bool = False) -> int:
        action, _ = self.model.predict(obs, deterministic=deterministic)
        return int(action)

    def store_transition(self, obs, action, reward, next_obs, done):
        self.model.replay_buffer.add(obs=obs, next_obs=next_obs,
                                      action=np.array([action]),
                                      reward=np.array([reward]),
                                      done=np.array([done]), infos=[{}])
        self.total_steps += 1
        if self.total_steps >= self.config["learning_starts"]:
            if self.total_steps % 4 == 0:
                self.model.train(gradient_steps=1, batch_size=self.config["batch_size"])

    def save(self, directory: str):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.model.save(str(Path(directory) / f"{self.service_name}_dqn"))

    def load(self, directory: str):
        self.model = DQN.load(str(Path(directory) / f"{self.service_name}_dqn"))

    def get_avg_reward(self):
        return 0.0

    def __repr__(self):
        return f"DQN({self.service_name}, dim={self.obs_dim}, steps={self.total_steps})"
