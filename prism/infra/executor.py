"""
Prism DagExecutor class

Table of Contents
- Imports
- Class definition
"""

###########
# Imports #
###########

# Standard library imports
from dataclasses import dataclass
from multiprocessing.dummy import Pool
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Union
import re

# Prism-specific imports
import prism.exceptions
from prism.infra import model as prism_model
from prism.infra import compiler as prism_compiler
from prism.infra.task_manager import PrismTaskManager
from prism.infra.hooks import PrismHooks
import prism.prism_logging
from prism.prism_logging import Event, fire_console_event
from prism.event_managers import base as base_event_manager
from prism.constants import INTERNAL_TASK_MANAGER_VARNAME, INTERNAL_HOOKS_VARNAME


####################
# Class definition #
####################

@dataclass
class ExecutorOutput:
    """
    Class for defining output of DagExecutor. Looks very similar to the output of the
    EventManager. We only need this because we need the error event and event list to
    cascade up to the PrismPipeline class.
    """
    success: int
    error_event: Optional[Event]
    event_list: List[Event]


class DagExecutor:
    """
    Class for introducing concurrency in DAG execution
    """

    def __init__(self,
        project_dir: Path,
        compiled_dag: prism_compiler.CompiledDag,
        user_arg_all_upstream: bool,
        user_arg_all_downstream: bool,
        threads: int,
        user_context: Dict[Any, Any] = {}
    ):
        self.project_dir = project_dir
        self.compiled_dag = compiled_dag

        # Extract attributes from compiled_dag instance
        self.compiled_models = self.compiled_dag.compiled_models
        self.nxdag = self.compiled_dag.nxdag
        self.topological_sort_model_names = self.compiled_dag.topological_sort
        self.topological_sort_full_path = self.compiled_dag.topological_sort_full_path
        self.user_arg_models = self.compiled_dag.user_arg_models
        self.user_arg_all_upstream = user_arg_all_upstream
        self.user_arg_all_downstream = user_arg_all_downstream
        self.user_context = user_context

        # Identify nodes not explicitly run and update (only if --all-upstream is False)
        if not self.user_arg_all_upstream and not self.user_arg_all_downstream:
            self.nodes_not_explicitly_run = list(
                set(self.topological_sort_model_names) - set(self.user_arg_models)
            )
        else:
            self.nodes_not_explicitly_run = []

        # Number of processes used to run concurrent tasks
        self.threads = threads

    def set_run_context(self, run_context: Dict[Any, Any]):
        """
        Set executor globals; needs to be called before `exec`

        args:
            run_context: globals for DAG execution
        """
        self.run_context = run_context

    def check_task_refs(self, model: prism_model.CompiledModel) -> List[str]:
        """
        Get task refs

        args:
            model: CompiledModel object
        returns:
            refs as a list of strings
        """
        if isinstance(model.refs, str):
            return [model.refs]
        else:
            if not isinstance(model.refs, list):
                raise prism.exceptions.CompileException(
                    message=f'invalid type `{type(model.refs)}`, must be list'
                )
            return model.refs

    def exec_single(self,
        full_tb: bool,
        model: prism_model.CompiledModel,
        task_manager: Union[int, PrismTaskManager],
        hooks: PrismHooks,
        user_context: Dict[Any, Any] = {}
    ) -> base_event_manager.EventManagerOutput:
        """
        Callback used to get results of model execution in Pool
        """
        # Keep track of current module in tasks manager
        task_manager.curr_module = re.sub(
            r'\.py$', '', model.model_relative_path.name
        )
        # Keep track of events
        event_list: List[Event] = []
        if task_manager == 0:
            base_event_manager.EventManagerOutput(0, None, event_list)
        model_name = model.task_var_name
        relative_path = model.model_relative_path

        # Boolean for whether to fire exec event for current script. We do not want to
        # fire the exec events if the user did not explicitly include the script in
        # their arguments
        fire_exec_events = model_name in self.user_arg_models \
            or self.user_arg_all_upstream \
            or self.user_arg_all_downstream

        # If all upstream models are to be run, the compute the idx and total using the
        # `dag`` list. Otherwise, if the script is explicitly included in the user's run
        # command then compute the idx and total using the `models` list. Otherwise,
        # set both to None.

        # First, sort the user arg models in the order in which they appear in the DAG
        models_idx = [
            self.topological_sort_model_names.index(m) for m in self.user_arg_models
        ]
        models_sorted = [x for _, x in sorted(zip(models_idx, self.user_arg_models))]

        # Define the idx and total
        if self.user_arg_all_upstream or self.user_arg_all_downstream:
            idx = self.topological_sort_model_names.index(model_name) + 1
            total = len(self.topological_sort_model_names)
        elif model_name in self.user_arg_models:
            idx = models_sorted.index(model_name) + 1
            total = len(models_sorted)
        else:
            idx = None
            total = None

        # Event manager. We want '__file__' to be the path to the un-compiled model.
        # Instances of DagExecutor will only be called within the project directory.
        # Therefore, __files__ should be models/{name of script}
        self.run_context['__file__'] = str(
            self.project_dir / f'models/{str(relative_path)}'
        )

        # Execute the model with appropriate number of retries
        retries, retry_delay_seconds = model.grab_retries_metadata()
        num_runs = 0
        num_expected_runs = retries + 1
        outputs = 0
        name = model.task_var_name

        # For testing, keep track of all events
        all_events = []

        while num_runs != num_expected_runs and outputs == 0:
            num_runs += 1
            if num_runs > 1:
                event_list = fire_console_event(
                    prism.prism_logging.DelayEvent(
                        name, retry_delay_seconds
                    ),
                    log_level='warn'
                )
                time.sleep(retry_delay_seconds)
                name = name + f' (RETRY {num_runs - 1})'

            # Only fire empty line if last retry has been executed
            fire_empty_line_events = num_runs == num_expected_runs
            script_manager = base_event_manager.BaseEventManager(
                idx=idx,
                total=total,
                name=name,
                full_tb=full_tb,
                func=model.exec
            )
            script_event_manager_result: base_event_manager.EventManagerOutput = script_manager.manage_events_during_run(  # noqa: E501
                event_list,
                fire_exec_events,
                fire_empty_line_events,
                run_context=self.run_context,
                task_manager=task_manager,
                hooks=hooks,
                explicit_run=relative_path not in self.nodes_not_explicitly_run,
                user_context=user_context
            )

            # All events
            all_events += script_event_manager_result.event_list

            # Set output
            outputs = script_event_manager_result.outputs

        # Now, update the event list of the output
        script_event_manager_result.event_list = all_events
        return script_event_manager_result

    def _cancel_connections(self, pool):
        """
        Given a pool, cancel all adapter connections and wait until all
        runners gentle terminates.
        """
        pool.close()
        pool.terminate()
        pool.join()

    def exec(self, full_tb: bool):
        """
        Execute DAG. Our general approach is as follows:
            1. Create a queue of the tasks (i.e., the models) that need to be executed.
            2. Create a pool with `n` processes
            3. While queue is not empty
                - Get task from the top of the queue
                - If task doesn't have any refs, add it to the pool
                - If task does have refs, then wait for those refs to finish before
                  adding it to pool
                - Once task has been added to queue, remove it from queue
        """

        # Keep track of events
        self.event_list: List[Event] = []

        def callback(result: base_event_manager.EventManagerOutput):
            task_manager = result.outputs
            error_event = result.event_to_fire
            runner_event_list = result.event_list

            # If task_manager==0, then we want to raise an error. However, if we do so
            # here, it'll get swallowed by the pool.
            if task_manager == 0:
                self._wait_and_return = True
                self.error_event = error_event
            self.task_manager = task_manager
            self.event_list += runner_event_list
            return

        # Execute all statements, stopping at first error
        self.task_manager = self.run_context[INTERNAL_TASK_MANAGER_VARNAME]
        self.hooks = self.run_context[INTERNAL_HOOKS_VARNAME]
        self._wait_and_return = False
        self.error_event = None

        # If single-threaded, just run the models in order
        if self.threads == 1:
            while self.compiled_models != []:
                curr: prism_model.CompiledModel = self.compiled_models.pop(0)
                result = self.exec_single(
                    full_tb,
                    curr,
                    self.task_manager,
                    self.hooks,
                    self.user_context
                )
                callback(result)
                if self.task_manager == 0:
                    return ExecutorOutput(0, self.error_event, self.event_list)

            # We need the error event and event list to cascade up to the PrismPipeline
            # class.
            return ExecutorOutput(1, self.error_event, self.event_list)

        # If the pool has multiple threads, then iterate through models and add them to
        # the Pool
        else:
            async_results = {}
            with Pool(processes=self.threads) as pool:
                while self.compiled_models != []:

                    # Get first model to execute
                    curr = self.compiled_models[0]
                    refs = self.check_task_refs(curr)

                    # If an error occurred, skip all remaining tasks
                    if self._wait_and_return:
                        break  # type: ignore

                    else:

                        # If no refs, then add model to pool
                        if len(refs) == 0:
                            res = pool.apply_async(
                                self.exec_single,
                                args=(full_tb, curr, self.task_manager, self.hooks, self.user_context),  # noqa: E501
                                callback=callback
                            )
                            async_results[curr.task_var_name] = res
                            self.compiled_models.pop(0)

                        # Since DAG is run in order, refs should have been added to pool
                        # before current task. Wait for upstream tasks
                        else:
                            for ref in refs:
                                async_results[ref].wait()

                            # If an error occurred, skip all remaining tasks
                            if self._wait_and_return:
                                break  # type: ignore
                            else:
                                res = pool.apply_async(
                                    self.exec_single,
                                    args=(full_tb, curr, self.task_manager, self.hooks, self.user_context),  # noqa: E501
                                    callback=callback
                                )
                                async_results[curr.task_var_name] = res
                                self.compiled_models.pop(0)
                pool.close()
                pool.join()

            # If error was found, then terminate pool
            if self._wait_and_return:
                self._cancel_connections(pool)
                # We need the error event and event list to cascade up to the
                # PrismPipeline class.
                return ExecutorOutput(0, self.error_event, self.event_list)

            # Otherwise, pool should close automatically
            else:
                # We need the error event and event list to cascade up to the
                # PrismPipeline class.
                return ExecutorOutput(1, self.error_event, self.event_list)
