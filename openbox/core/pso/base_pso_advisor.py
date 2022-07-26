import abc

import random

from typing import *

import numpy as np
from openbox.utils.util_funcs import check_random_state
from openbox.utils.logging_utils import get_logger
from openbox.utils.history_container import HistoryContainer, MOHistoryContainer
from openbox.utils.constants import MAXINT, SUCCESS
from openbox.core.base import Observation

from ConfigSpace import ConfigurationSpace, Configuration


class BasePSOAdvisor(abc.ABC):
    """
    This is the base class using particle swarm optimization.
    An instance of this class may be used as an advisor somewhere else.
    This is an abstract class. Define a subclass of this to implement an advisor.
    """

    def __init__(self, config_space: ConfigurationSpace,
                 num_objs = 1,
                 num_constraints = 0,
                 population_size = 30,
                 batch_size = 1,
                 output_dir = 'logs',
                 task_id = 'default_task_id',
                 random_state = None):

        # System Settings.
        self.rng = check_random_state(random_state)
        self.output_dir = output_dir
        self.logger = get_logger(self.__class__.__name__)

        # Objectives Settings
        self.num_objs = num_objs
        self.num_constraints = num_constraints
        self.config_space = config_space
        self.config_space_seed = self.rng.randint(MAXINT)
        self.config_space.seed(self.config_space_seed)

        # Init parallel settings
        self.batch_size = batch_size
        self.init_num = batch_size  # for compatibility in pSMBO
        self.running_configs = list()

        # Start initialization for PSO variables.
        self.all_configs = set()
        self.population: List[Union[Dict, Individual]] = list()
        self.population_size = population_size
        assert self.population_size is not None

        # init history container
        if num_objs == 1:
            self.history_container = HistoryContainer(task_id, self.num_constraints, config_space = self.config_space)
        else:
            self.history_container = MOHistoryContainer(task_id, self.num_objs, self.num_constraints)

    def get_suggestions(self):
        """
        Abstract. An advisor must implement this.
        Call this to get suggestions from the advisor.
        The caller should evaluate this configuration and then call update_observations to send the result back.
        """
        raise NotImplementedError

    def update_observations(self, observation: Observation):
        """
        Abstract. An advisor must implement this.
        Call this to send the result back to advisor.
        It should be guaranteed that the configuration evaluated in this observation is got by calling
        get_suggestion earlier on the same advisor.
        """
        raise NotImplementedError

    def sample_random_config(self, sample_space, excluded_configs = None):
        if excluded_configs is None:
            excluded_configs = set()

        sample_cnt = 0
        max_sample_cnt = 1000
        while True:
            config = sample_space.sample_configuration()
            sample_cnt += 1
            if config not in excluded_configs:
                break
            if sample_cnt >= max_sample_cnt:
                self.logger.warning('Cannot sample non duplicate configuration after %d iterations.' % max_sample_cnt)
                break
        return config

    def get_history(self):
        return self.history_container


class Individual:
    def __init__(self,
                 pos: np.ndarray,
                 vel: np.ndarray,
                 perf: Union[int, float, List[float]],
                 constraints_satisfied: bool = True,
                 data: Optional[Dict] = None,
                 **kwargs):
        self.vel = vel
        self.pos = pos
        if isinstance(perf, float) or isinstance(perf, int):
            self.dim = 1
        else:
            self.dim = len(perf)

        self.perf = perf
        self.constraints_satisfied = constraints_satisfied

        self.data = kwargs
        if data is not None:
            for x in data:
                self.data[x] = data[x]

    def perf_1d(self):
        assert self.dim == 1
        return self.perf if isinstance(self.perf, float) else self.perf[0]

    # For compatibility
    def __getitem__(self, item):
        if item == 'vel':
            return self.vel
        elif item == 'pos':
            return self.pos
        elif item == 'perf':
            return self.perf
        elif item == 'constraints_satisfied':
            return self.constraints_satisfied
        else:
            return self.data[item]

    def __setitem__(self, item, val):
        if item == 'vel':
            self.vel = val
        elif item == 'pos':
            self.pos = val
        elif item == 'perf':
            self.perf = val
        elif item == 'constraints_satisfied':
            self.constraints_satisfied = val
        else:
            self.data[item] = val


def pareto_sort(population: List[Individual],
                selection_strategy = 'random', ascending = False) -> List[Individual]:
    t = pareto_best(population, count_ratio = 1.0, selection_strategy = selection_strategy)
    if ascending:
        t.reverse()
    return t


def pareto_best(population: List[Individual],
                count: Optional[int] = None,
                count_ratio: Optional[float] = None,
                selection_strategy = 'random') -> List[Individual]:
    assert not (count is None and count_ratio is None)
    assert selection_strategy in ['random']

    if count is None:
        count = max(1, int(len(population) * count_ratio))

    remain = [x for x in population]

    if remain[0].dim == 1:
        remain.sort(key = lambda a: a.perf_1d())
        return remain[:count]

    res = []
    while count > 0:
        front = pareto_frontier(remain)
        assert len(front) > 0
        if selection_strategy == 'random':
            random.shuffle(front)
        if count >= len(front):
            res.extend(front)
            remain = [x for x in remain if x not in front]
            count -= len(front)
        else:
            res.extend(front[:count])
            count = 0

    return res


def pareto_layers(population: List[Individual]) -> List[List[Individual]]:
    remain = [x for x in population]

    res = []
    while remain:
        front = pareto_frontier(remain)
        assert len(front) > 0
        res.append(front)
        remain = [x for x in remain if x not in front]

    return res


# Naive Implementation
def pareto_frontier(population: List[Individual]) -> List[Individual]:
    if isinstance(population[0].perf, float):
        return [x for x in population if
                not [y for y in population if y.perf < x.perf]]
    return [x for x in population if
            not [y for y in population if not [i for i in range(len(x.perf)) if y.perf[i] >= x.perf[i]]]]


def constraint_check(constraint, positive_numbers = False) -> bool:
    if constraint is None:
        return True
    elif isinstance(constraint, bool):
        return constraint
    elif isinstance(constraint, float) or isinstance(constraint, int):
        return constraint >= 0 if positive_numbers else constraint <= 0
    elif isinstance(constraint, Iterable):
        return not [x for x in constraint if not constraint_check(x, positive_numbers)]
    else:
        return bool(constraint)