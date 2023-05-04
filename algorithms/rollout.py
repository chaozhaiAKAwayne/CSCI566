"""
Runs rollouts (RolloutRunner class) and collects transitions using Rollout class.
"""

from collections import defaultdict

import numpy as np
import cv2

from ..utils.logger import logger
from ..utils.info_dict import Info
from ..utils.gym_env import get_non_absorbing_state, zero_value


class Rollout(object):
    """
    Rollout storing an episode.
    """

    def __init__(self):
        """ Initialize buffer. """
        self._history = defaultdict(list)

    def add(self, data):
        """ Add a transition @data to rollout buffer. """
        for key, value in data.items():
            self._history[key].append(value)

    def get(self):
        """ Returns rollout buffer and clears buffer. """
        batch = {}
        batch["ob"] = self._history["ob"]
        batch["ob_next"] = self._history["ob_next"]
        batch["ac"] = self._history["ac"]
        batch["ac_before_activation"] = self._history["ac_before_activation"]
        batch["done"] = self._history["done"]
        batch["done_mask"] = self._history["done_mask"]
        batch["rew"] = self._history["rew"]
        self._history = defaultdict(list)
        return batch


class RolloutRunner(object):
    """
    Run rollout given an environment and policy.
    """

    def __init__(self, config, env, env_eval, agent):
        """
        Args:
            config: configurations for the environment.
            env: training environment.
            env_eval: testing environment.
            agent: policy.
        """
        self._config = config
        self._env = env
        self._env_eval = env_eval
        self._agent = agent
        self._exclude_rollout_log = ["episode_success_state"]

    def run(
        self,
        is_train=True,
        every_steps=None,
        every_episodes=None,
        log_prefix="",
        step=0,
    ):
        """
        Collects trajectories and yield every @every_steps/@every_episodes.

        Args:
            is_train: whether rollout is for training or evaluation.
            every_steps: if not None, returns rollouts @every_steps
            every_episodes: if not None, returns rollouts @every_epiosdes
            log_prefix: log as @log_prefix rollout: %s
        """
        if every_steps is None and every_episodes is None:
            raise ValueError("Both every_steps and every_episodes cannot be None")

        config = self._config
        env = self._env if is_train else self._env_eval
        agent = self._agent
        il = hasattr(agent, "predict_reward")

        # initialize rollout buffer
        rollout = Rollout()
        reward_info = Info()
        ep_info = Info()
        episode = 0

        while True:
            done = False
            ep_len = 0
            ep_rew = 0
            ep_rew_rl = 0
            if il:
                ep_rew_il = 0
            ob_next = env.reset()

            # run rollout
            while not done:
                ob = ob_next

                # sample action from policy
                if step < config.warm_up_steps:
                    ac, ac_before_activation = env.action_space.sample(), 0
                else:
                    ac, ac_before_activation = agent.act(ob, is_train=is_train)

                # take a step
                ob_next, reward, done, info = env.step(ac)

                if il:
                    reward_il = agent.predict_reward(ob, ob_next, ac)
                    reward_rl = (
                        (1 - config.gail_env_reward) * reward_il
                        + config.gail_env_reward * reward * config.reward_scale
                    )
                else:
                    reward_rl = reward * config.reward_scale

                step += 1
                ep_len += 1
                ep_rew += reward
                ep_rew_rl += reward_rl
                if il:
                    ep_rew_il += reward_il

                if done and ep_len < env.max_episode_steps:
                    done_mask = 0  # -1 absorbing, 0 done, 1 not done
                else:
                    done_mask = 1

                rollout.add(
                    {
                        "ob": ob,
                        "ob_next": ob_next,
                        "ac": ac,
                        "ac_before_activation": ac_before_activation,
                        "done": done,
                        "rew": reward,
                        "done_mask": done_mask,  # -1 absorbing, 0 done, 1 not done
                    }
                )

                reward_info.add(info)

                if config.absorbing_state and done_mask == 0:
                    absorbing_state = env.get_absorbing_state()
                    absorbing_action = zero_value(env.action_space)
                    rollout._history["ob_next"][-1] = absorbing_state
                    rollout.add(
                        {
                            "ob": absorbing_state,
                            "ob_next": absorbing_state,
                            "ac": absorbing_action,
                            "ac_before_activation": absorbing_action,
                            "rew": 0.0,
                            "done": 0,
                            "done_mask": -1,  # -1 absorbing, 0 done, 1 not done
                        }
                    )

                if every_steps is not None and step % every_steps == 0:
                    yield rollout.get(), ep_info.get_dict(only_scalar=True)

            # compute average/sum of information
            reward_info_dict = reward_info.get_dict(reduction="sum", only_scalar=True)
            reward_info_dict.update({"len": ep_len, "rew": ep_rew, "rew_rl": ep_rew_rl})
            if il:
                reward_info_dict.update({"rew_il": ep_rew_il})
            ep_info.add(reward_info_dict)

            logger.info(
                "rollout: %s",
                {
                    k: v
                    for k, v in reward_info_dict.items()
                    if k not in self._exclude_rollout_log and np.isscalar(v)
                },
            )

            episode += 1
            if every_episodes is not None and episode % every_episodes == 0:
                yield rollout.get(), ep_info.get_dict(only_scalar=True)

    def run_episode(self, is_train=True, record_video=False):
        """
        Runs one episode and returns the rollout (mainly for evaluation).

        Args:
            is_train: whether rollout is for training or evaluation.
            record_video: record video of rollout if True.
        """
        config = self._config
        env = self._env if is_train else self._env_eval
        agent = self._agent
        il = hasattr(agent, "predict_reward")

        # initialize rollout buffer
        rollout = Rollout()
        reward_info = Info()

        done = False
        ep_len = 0
        ep_rew = 0
        ep_rew_rl = 0
        if il:
            ep_rew_il = 0

        ob_next = env.reset()

        record_frames = []
        if record_video:
            record_frames.append(self._store_frame(env, ep_len, ep_rew))

        # run rollout
        while not done:
            ob = ob_next

            # sample action from policy
            ac, ac_before_activation = agent.act(ob, is_train=is_train)

            # take a step
            ob_next, reward, done, info = env.step(ac)

            if il:
                reward_il = agent.predict_reward(ob, ob_next, ac)
                reward_rl = (
                    1 - config.gail_env_reward
                ) * reward_il + config.gail_env_reward * reward * config.reward_scale
            else:
                reward_rl = reward * config.reward_scale

            rollout.add(
                {
                    "ob": ob,
                    "ob_next": ob_next,
                    "ac": ac,
                    "ac_before_activation": ac_before_activation,
                    "done": done,
                    "rew": reward,
                }
            )

            ep_len += 1
            ep_rew += reward
            ep_rew_rl += reward_rl
            if il:
                ep_rew_il += reward_il

            reward_info.add(info)
            if record_video:
                frame_info = info.copy()
                if il:
                    frame_info.update(
                        {
                            "ep_rew_il": ep_rew_il,
                            "rew_il": reward_il,
                            "rew_rl": reward_rl,
                        }
                    )
                record_frames.append(self._store_frame(env, ep_len, ep_rew, frame_info))

        # add last observation
        rollout.add({"ob": ob_next})

        # compute average/sum of information
        ep_info = {"len": ep_len, "rew": ep_rew, "rew_rl": ep_rew_rl}
        if il:
            ep_info["rew_il"] = ep_rew_il
        if "episode_success_state" in reward_info.keys():
            ep_info["episode_success_state"] = reward_info["episode_success_state"]
        ep_info.update(reward_info.get_dict(reduction="sum", only_scalar=True))

        logger.info(
            "rollout: %s",
            {
                k: v
                for k, v in ep_info.items()
                if k not in self._exclude_rollout_log and np.isscalar(v)
            },
        )
        return rollout.get(), ep_info, record_frames

    def _store_frame(self, env, ep_len, ep_rew, info={}):
        """ Renders a frame. """
        color = (200, 200, 200)

        # render video frame
        frame = env.render("rgb_array")
        if len(frame.shape) == 4:
            frame = frame[0]
        if np.max(frame) <= 1.0:
            frame *= 255.0
        frame = frame.astype(np.uint8)

        h, w = frame.shape[:2]
        if h < 512:
            h, w = 512, 512
            frame = cv2.resize(frame, (h, w))
        frame = np.concatenate([frame, np.zeros((h, w, 3))], 0)
        scale = h / 512

        # add caption to video frame
        if self._config.record_video_caption:
            text = "{:4} {}".format(ep_len, ep_rew)
            font_size = 0.4 * scale
            thickness = 1
            offset = int(12 * scale)
            x, y = int(5 * scale), h + int(10 * scale)
            cv2.putText(
                frame,
                text,
                (x, y),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_size,
                (255, 255, 0),
                thickness,
                cv2.LINE_AA,
            )
            for i, k in enumerate(info.keys()):
                v = info[k]
                key_text = "{}: ".format(k)
                (key_width, _), _ = cv2.getTextSize(
                    key_text, cv2.FONT_HERSHEY_SIMPLEX, font_size, thickness
                )

                cv2.putText(
                    frame,
                    key_text,
                    (x, y + offset * (i + 2)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    font_size,
                    (66, 133, 244),
                    thickness,
                    cv2.LINE_AA,
                )

                cv2.putText(
                    frame,
                    str(v),
                    (x + key_width, y + offset * (i + 2)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    font_size,
                    (255, 255, 255),
                    thickness,
                    cv2.LINE_AA,
                )

        return frame
