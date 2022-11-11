import math
import random
import time
import typing
from copy import deepcopy
from typing import Optional, Callable, List, Type, Union, Any, Tuple, Dict

import numpy as np
from ConfigSpace import ConfigurationSpace, Configuration, UniformFloatHyperparameter, CategoricalHyperparameter, \
    OrdinalHyperparameter

from openbox import Advisor
from openbox.core.ea.regularized_ea_advisor import RegularizedEAAdvisor
from openbox.acquisition_function import AbstractAcquisitionFunction
from openbox.core.base import build_acq_func, build_surrogate, Observation, build_optimizer
from openbox.core.ea.base_ea_advisor import Individual
from openbox.core.ea.base_modular_ea_advisor import ModularEAAdvisor
from openbox.core.highdim.linebo_advisor import LineBOAdvisor
from openbox.surrogate.base.base_model import AbstractModel
from openbox.utils.config_space import convert_configurations_to_array
from openbox.utils.history_container import HistoryContainer, MOHistoryContainer
from openbox.utils.multi_objective import NondominatedPartitioning, get_chebyshev_scalarization
from openbox.utils.util_funcs import check_random_state


def nd_range(*args):
    """
    There should be some system function that have implemented this. However, I didn't find it.

    Example:
    list(nd_range(2,3)) -> [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    """
    size = args[0] if isinstance(args[0], tuple) else args
    if len(size) == 1:
        for i in range(size[0]):
            yield (i,)
    else:
        for i in range(size[0]):
            for j in nd_range(size[1:]):
                yield (i,) + j


class DefaultBeta:
    """
    The class to evaluate beta with given turn number t.
    The value of beta is used for predictive interval [mean - beta ** (1/2) * std, mean + beta ** (1/2) * std].

    b is a bound for RKHS norm of objective function f.
    sz is the size of sampled points.
    delta is the allowed failure probability.
    c is the constant where gamma = c * sz, as it's said that gamma has sublinear dependence of sz for our GP kernels.
    """

    def __init__(self, b: float, sz: int, delta: float, c: float = 1.0):
        self.b = b
        self.sz = sz
        self.delta = delta
        self.c = c

    def __call__(self, t: float):
        gamma = self.c * self.sz
        return 2 * self.b + 300 * gamma * (math.log(t / self.delta) ** 3)


class SetManager:
    """
    Maintain a set of n-d linspaced points.
    Use boolean arrays to determine whether they're in some sets (s, g, m, vis)
    Also stores their GP prediction info (upper, lower)
    """

    def __init__(self, config_space: ConfigurationSpace, size: Tuple[int]):
        self.config_space = config_space
        self.dim = len(size)
        self.size = size

        self.s_set = np.zeros(size, dtype=np.bool)  # Safe set
        self.g_set = np.zeros(size, dtype=np.bool)  # Expander set
        self.m_set = np.zeros(size, dtype=np.bool)  # Minimizer set

        self.vis_set = np.zeros(size, dtype=np.bool)  # Set of evaluated points. Added this to avoid repeated configs.

        self.upper_conf = np.ones(size, dtype=np.float) * 1e100
        self.lower_conf = np.ones(size, dtype=np.float) * -1e100

        self.tmp = np.zeros(size, dtype=np.int)

    def _presum(self):
        if self.dim == 1:
            for i in range(1, self.size[0]):
                self.tmp[i] += self.tmp[i - 1]
        else:
            for i in range(1, self.size[0]):
                self.tmp[i, :] += self.tmp[i - 1, :]
            for i in range(1, self.size[1]):
                self.tmp[:, i] += self.tmp[:, i - 1]

    def _query_range(self, l: Tuple[int], r: Tuple[int]):
        if self.dim == 1:
            return self.tmp[r] - (0 if l[0] == 0 else self.tmp[l[0] - 1])
        else:
            x1, y1 = l
            x2, y2 = r
            ans = self.tmp[x2, y2]
            if x1 > 0:
                ans -= self.tmp[x1 - 1, y2]
                if y1 > 0:
                    ans += self.tmp[x1 - 1, y1 - 1]
            if y1 > 0:
                ans -= self.tmp[x2, y1 - 1]
            return ans

    def _add_range(self, l: Tuple[int], r: Tuple[int]):
        if self.dim == 1:
            self.tmp[l] += 1
            if r[0] < self.size[0] - 1:
                self.tmp[r[0] + 1] -= 1
        else:
            x1, y1 = l
            x2, y2 = r
            self.tmp[x1, y1] += 1
            if x2 < self.size[0] - 1:
                self.tmp[x2 + 1, y1] -= 1
                if y2 < self.size[1] - 1:
                    self.tmp[x2 + 1, y2 + 1] += 1
            if y2 < self.size[1] - 1:
                self.tmp[x1, y2 + 1] -= 1

    def nearest(self, x0: np.ndarray):
        return tuple(int(x0[i] * (self.size[i] - 1) + 0.5) for i in range(self.dim))

    def update_bounds(self, i: Tuple[int], m: float, v: float, b: float):
        i = tuple(i)
        self.upper_conf[i] = min(self.upper_conf[i], m + v * b)
        self.lower_conf[i] = max(self.lower_conf[i], m - v * b)

    def update_s_set(self, h: float, l: float):
        self.tmp.fill(0)
        for i in nd_range(self.size):
            if self.s_set[i]:
                maxd = (h - self.upper_conf[i]) / l
                # print(maxd)
                if maxd > 0:
                    t = self.dim ** 0.5
                    mn = tuple(max(math.ceil(i[j] - maxd * (self.size[j] - 1) / t), 0) for j in range(self.dim))
                    mx = tuple(min(math.floor(i[j] + maxd * (self.size[j] - 1) / t), self.size[j] - 1) for j in
                               range(self.dim))
                    self._add_range(mn, mx)

        self._presum()
        self.s_set |= (self.tmp > 0)

    def update_g_set(self, h: float, l: float):
        self.g_set.fill(False)
        self.tmp.fill(1)
        self.tmp -= self.s_set
        self._presum()

        for i in nd_range(self.size):
            if self.s_set[i]:
                maxd = (h - self.lower_conf[i]) / l
                if maxd > 0:
                    t = self.dim ** 0.5
                    mn = tuple(max(math.ceil(i[j] - maxd * (self.size[j] - 1) / t), 0) for j in range(self.dim))
                    mx = tuple(min(math.floor(i[j] + maxd * (self.size[j] - 1) / t), self.size[j] - 1) for j in
                               range(self.dim))

                    if self._query_range(mn, mx) > 0:
                        self.g_set[i] = True

    def update_m_set(self, minu: float):
        self.m_set = self.s_set & (self.lower_conf <= minu)

    def get_array(self, coord: Tuple[int]):
        if isinstance(coord, Configuration):
            coord = coord.get_array()

        return np.array(list(coord[i] / float(self.size[i] - 1) for i in range(self.dim)))

    def get_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Get the arrays of all config in s_set and their coordinates. For GP prediction of each turn.
        """
        arrays = []
        ids = []
        for i in nd_range(self.size):
            if self.s_set[i]:
                arrays.append(self.get_array(i))
                ids.append(i)

        return np.array(arrays), np.array(ids)

    def get_config(self, coord: Tuple[int]):
        return Configuration(self.config_space, vector=self.get_array(coord))


class SafeOptAdvisor:

    def __init__(self, config_space: ConfigurationSpace,
                 num_objs=1,
                 num_constraints=1,
                 task_id='default_task_id',
                 random_state=None,

                 surrogate: Union[str, AbstractModel] = 'gp',

                 sample_size: Union[int, Tuple] = 40000,
                 seed_set: Union[None, List[Configuration], np.ndarray] = None,

                 lipschitz: float = 20.0,
                 threshold: float = 1.0,
                 beta: Union[float, Callable[[float], float]] = 2.0

                 ):
        self.num_objs = num_objs
        # May support multi-obj in the future.
        assert self.num_objs == 1

        self.num_constraints = num_constraints
        # Let's assume that the only constraint is x - h.
        assert self.num_constraints == 1

        self.config_space = config_space
        self.dim = len(config_space.keys())
        self.rng = check_random_state(random_state)
        self.task_id = task_id

        if isinstance(surrogate, str):
            self.objective_surrogate: AbstractModel = build_surrogate(surrogate, config_space, self.rng or random, None)
        elif isinstance(surrogate, AbstractModel):
            self.objective_surrogate = surrogate

        if isinstance(sample_size, int):
            sample_size = (int(sample_size ** (1 / self.dim)),) * self.dim

        self.sets = SetManager(self.config_space, sample_size)

        if seed_set is None:
            raise ValueError("Seed set must not be None!")
        elif isinstance(seed_set, list):
            self.seed_set = seed_set
        else:
            self.seed_set = [Configuration(config_space, vector=seed_set[i]) for i in range(seed_set.shape[0])]

        for x in self.seed_set:
            self.sets.s_set[self.sets.nearest(x.get_array())] = True

        self.threshold = threshold
        self.lipschitz = lipschitz
        if callable(beta):
            self.beta = beta
        else:
            self.beta = lambda t: beta

        self.history_container = HistoryContainer(task_id, 0, self.config_space) if num_objs == 1 else \
            MOHistoryContainer(task_id, num_objs, 0, self.config_space)

        arrays = self.sets.get_arrays()[0]
        self.to_eval: List[Configuration] = [Configuration(config_space, vector=arrays[i]) for i in
                                             range(arrays.shape[0])]

        self.current_turn = 0

    def debug(self, arr):
        if self.dim == 1:
            s = "".join("_" if not i else "|" for i in arr)
            print(s)
        else:
            print("-" * (self.sets.size[1] + 2))
            for i in range(self.sets.size[0]):
                print("|" + "".join("#" if j else " " for j in arr[i]) + "|")
            print("-" * (self.sets.size[1] + 2))

    def get_suggestion(self):
        if len(self.to_eval) == 0:

            X = convert_configurations_to_array(self.history_container.configurations)
            Y = self.history_container.get_transformed_perfs()

            self.objective_surrogate.train(X, Y[:, 0] if Y.ndim == 2 else Y)

            self.current_turn += 1
            beta_sqrt = self.beta(self.current_turn) ** 0.5

            arrays, ids = self.sets.get_arrays()
            mean, var = self.objective_surrogate.predict(arrays)

            # print(arrays, ids)
            # print(mean, var)

            for i in range(ids.shape[0]):
                self.sets.update_bounds(ids[i], mean[i].item(), var[i].item(), beta_sqrt)
                # print(self.sets.upper_conf[ids[i]])
                # print(self.sets.lower_conf[ids[i]])

            self.sets.update_s_set(self.threshold, self.lipschitz)

            # self.debug(self.sets.s_set)

            self.sets.update_g_set(self.threshold, self.lipschitz)

            minu = np.min(self.sets.upper_conf[self.sets.s_set])

            self.sets.update_m_set(minu)

            retx = None
            maxv = -1e100

            for i in nd_range(self.sets.size):
                condition = (self.sets.g_set[i] or self.sets.m_set[i]) and not self.sets.vis_set[i]
                # if self.current_turn % 3 == 0:
                #     condition = self.sets.m_set[i] and not self.sets.vis_set[i]
                if condition:
                    w = self.sets.upper_conf[i] - self.sets.lower_conf[i]
                    if w > maxv:
                        maxv = w
                        retx = i

            if retx is not None:
                self.to_eval = [self.sets.get_config(retx)]
            else:
                # print("SELECTING RANDOM CONFIG")
                possibles = self.sets.s_set & ~self.sets.vis_set
                if not np.any(possibles):
                    temp = self.sets.vis_set
                    temp[1:] |= temp[:-1]
                    temp[:-1] |= temp[1:]

                    if self.dim == 2:
                        temp[:, 1:] |= temp[:, :-1]
                        temp[:, :-1] |= temp[:, 1:]

                    possibles = temp & ~self.sets.vis_set

                if not np.any(possibles):
                    possibles = ~self.sets.vis_set

                # self.debug(possibles)

                coords = np.array(list(nd_range(self.sets.size)))[possibles.flatten()]

                self.to_eval = [self.sets.get_config(coords[self.rng.randint(0, coords.shape[0])])]

        return self.to_eval[0]

    def update_observation(self, observation: Observation):

        if observation.config in self.to_eval:
            self.to_eval.remove(observation.config)
            self.sets.vis_set[self.sets.nearest(observation.config.get_array())] = True

        observation.constraints = None

        return self.history_container.update_observation(observation)

    def get_history(self):
        return self.history_container
