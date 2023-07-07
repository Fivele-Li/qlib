import os

from pathlib import Path
import io
from typing import Any, List, Union
import ruamel.yaml as yaml
import abc
import re
import subprocess
import platform
import inspect

from qlib.finco.llm import APIBackend
from qlib.finco.tpl import get_tpl_path
from qlib.finco.prompt_template import PromptTemplate
from qlib.contrib.analyzer import HFAnalyzer, SignalAnalyzer
from qlib.workflow import R
from qlib.finco.log import FinCoLog, LogColors
from qlib.finco.conf import Config

COMPONENT_LIST = ["Dataset", "DataHandler", "Model", "Record", "Strategy", "Backtest"]


class Task:
    """
    The user's intention, which was initially represented by a prompt, is achieved through a sequence of tasks.
    This class doesn't have to be abstract, but it is abstract in the sense that it is not supposed to be instantiated directly because it doesn't have any implementation.

    Some thoughts:
    - Do we have to split create a new concept of Action besides Task?
        - Most actions directly modify the disk, with their interfaces taking in and outputting text. The LLM's interface similarly takes in and outputs text.
        - Some actions will run some commands.

    Maybe we can just categorizing tasks by following?
    - Planning task (it is at a high level and difficult to execute directly; therefore, it should be further divided):
    - Action Task
        - CMD Task: it is expected to run a cmd
        - Edit Task: it is supposed to edit the code base directly.
    """

    def __init__(self) -> None:
        self._context_manager = None
        self.prompt_template = PromptTemplate()
        self.executed = False
        self.continuous = Config().continuous_mode
        self.logger = FinCoLog()

    def summarize(self) -> str:
        """After the execution of the task, it is supposed to generated some context about the execution"""
        """This function might be converted to abstract method in the future"""
        self.logger.info(f"{self.__class__.__name__}: The task has nothing to summarize", plain=True)

    def assign_context_manager(self, context_manager):
        """assign the workflow context manager to the task"""
        """then all tasks can use this context manager to share the same context"""
        self._context_manager = context_manager

    def save_chat_history_to_context_manager(self, user_input, response, system_prompt, target_component="None"):
        chat_history = self._context_manager.get_context("chat_history")
        if chat_history is None:
            chat_history = {}
        if self.__class__.__name__ not in chat_history:
            chat_history[self.__class__.__name__] = {}
        if target_component not in chat_history[self.__class__.__name__]:
            chat_history[self.__class__.__name__][target_component] = [{"role": "system", "content": system_prompt}]
        chat_history[self.__class__.__name__][target_component].append({"role": "user", "content": user_input})
        chat_history[self.__class__.__name__][target_component].append({"role": "assistant", "content": response})
        self._context_manager.update_context("chat_history", chat_history)

    @abc.abstractclassmethod
    def execute(self, **kwargs) -> Any:
        """The execution results of the task"""
        """All sub classes should implement the execute method to determine the next task"""
        raise NotImplementedError

    def interact(self, prompt: str, **kwargs) -> Any:
        """
            The user can interact with the task. This method only handle business in current task. It will return True
            while continuous is True. This method will return user input if input cannot be parsed as 'yes' or 'no'.
            @return True, False, str
        """
        self.logger.info(title="Interact")
        if self.continuous:
            return True

        try:
            answer = input(prompt)
        except KeyboardInterrupt:
            self.logger.info("User has exited the program.")
            exit()

        if answer.lower().strip() in ["y", "yes"]:
            return True
        elif answer.lower().strip() in ["n", "no"]:
            return False
        else:
            return answer

    @property
    def system(self):
        return self.prompt_template.get(self.__class__.__name__ + "_system")

    @property
    def user(self):
        return self.prompt_template.get(self.__class__.__name__ + "_user")

    def __str__(self):
        return self.__class__.__name__


class WorkflowTask(Task):
    """This task is supposed to be the first task of the workflow"""

    def __init__(self) -> None:
        super().__init__()

    def execute(self) -> List[Task]:
        """make the choice which main workflow (RL, SL) will be used"""
        user_prompt = self._context_manager.get_context("user_prompt")
        prompt_workflow_selection = self.user.render(user_prompt=user_prompt)
        response = APIBackend().build_messages_and_create_chat_completion(
            prompt_workflow_selection, self.system.render()
        )
        self.save_chat_history_to_context_manager(
            prompt_workflow_selection, response, self.system.render()
        )
        workflow = response.split(":")[1].strip().lower()
        self.executed = True
        self._context_manager.set_context("workflow", workflow)

        confirm = self.interact(
            f"The workflow has been determined to be: "
            f"{LogColors().render(workflow, color=LogColors.YELLOW, style=LogColors.BOLD)}\n"
            f"Enter 'y' to authorise command,'s' to run self-feedback commands, "
            f"'n' to exit program, or enter feedback for WorkflowTask: "
        )
        if confirm is False:
            return []

        if workflow == "supervised learning":
            return [SLPlanTask()]
        elif workflow == "reinforcement learning":
            return [RLPlanTask()]
        else:
            raise ValueError(f"The workflow: {workflow} is not supported")


class PlanTask(Task):
    pass


class SLPlanTask(PlanTask):
    def __init__(self, replan=False, error=None) -> None:
        super().__init__()
        self.replan = replan
        self.error = error

    def execute(self):
        workflow = self._context_manager.get_context("workflow")
        assert (workflow == "supervised learning"), "The workflow is not supervised learning"

        user_prompt = self._context_manager.get_context("user_prompt")
        assert user_prompt is not None, "The user prompt is not provided"
        prompt_plan_all = self.user.render(user_prompt=user_prompt)
        former_messages = []
        if self.replan:
            prompt_plan_all = f"your choice of predefined classes cannot be initialized.\nPlease rewrite the plan and answer with exact required format in system prompt and reply with no more explainations.\nThe error message: {self.error}. Please correct the former answer accordingly."
            former_messages = self._context_manager.get_context("chat_history")[self.__class__.__name__]['None'][1:]
        response = APIBackend().build_messages_and_create_chat_completion(
            prompt_plan_all, self.system.render(), former_messages=former_messages
        )
        self.save_chat_history_to_context_manager(
            prompt_plan_all, response, self.system.render()
        )
        if "components" not in response.lower():
            self.logger.warning(
                "The response is not in the correct format, which probably means the answer is not correct"
            )
        
        decision_pattern = re.compile("\((.*?)\)")
        class_pattern = re.compile("{(.*?)}-{(.*?)}")
        new_task = []
        # 1) create a workspace
        # TODO: we have to make choice between `sl` and  `sl-cfg`
        new_task.append(
            CMDTask(
                cmd_intention=f"Copy folder from {get_tpl_path() / 'sl'} to {self._context_manager.get_context('workspace')}"
            )
        )

        # 2) CURD on the workspace
        for name in COMPONENT_LIST:
            target_line = [line for line in response.split("\n") if name in line]
            assert len(target_line) == 1, f"The {name} is not found in the response"
            target_line = target_line[0].strip("- ")
            decision = re.search(decision_pattern, target_line)
            assert decision is not None, f"The decision of {name} is not found"
            decision = decision.group(1)
            classes = re.findall(class_pattern, target_line)
            try:
                for module_path, class_name in classes:
                    exec(f"from {module_path} import {class_name}")
            except ImportError as e:
                self.logger.warning(f"The {class_name} is not found in {module_path}")
                return [SLPlanTask(replan=True, error=str(e))]
            self._context_manager.set_context(f"{name}_decision", decision)
            self._context_manager.set_context(f"{name}_classes", classes)
            self._context_manager.set_context(f"{name}_plan", target_line)

            assert decision in ["Default", "Personized"], f"The decision of {name} is not correct"
            if decision == "Default":
                new_task.extend([HyperparameterActionTask(name), ConfigActionTask(name), YamlEditTask(name)])
            elif decision == "Personized":
                # TODO open ImplementActionTask to let GPT write code
                new_task.extend([HyperparameterActionTask(name), ConfigActionTask(name), YamlEditTask(name)])
                # new_task.extend([HyperparameterActionTask(name), ConfigActionTask(name), ImplementActionTask(name), CodeDumpTask(name), YamlEditTask(name)])
        return new_task


class RLPlanTask(PlanTask):
    def __init__(
            self,
    ) -> None:
        super().__init__()
        self.logger.error("The RL task is not implemented yet")
        exit()

    def execute(self):
        """
        return a list of interested tasks
        Copy the template project maybe a part of the task
        """
        return []


class TrainTask(Task):
    """
    This train task is responsible for training model configure by yaml file.
    """

    def __init__(self):
        super().__init__()
        self._output = None

    def execute(self):
        workflow_config = (
            self._context_manager.get_context("workflow_config")
            if self._context_manager.get_context("workflow_config")
            else "workflow_config.yaml"
        )

        workspace = self._context_manager.get_context("workspace")
        workflow_path = workspace.joinpath(workflow_config)
        with workflow_path.open() as f:
            workflow = yaml.safe_load(f)
        self._context_manager.set_context("workflow_yaml", workflow)

        confirm = self.interact(f"I select this workflow file: "
                                f"{LogColors().render(workflow_path, color=LogColors.YELLOW, style=LogColors.BOLD)}\n"
                                f"{yaml.dump(workflow, default_flow_style=False)}"
                                f"Are you sure you want to use? yes(Y/y), no(N/n):")
        if confirm is False:
            return []

        command = ["qrun", str(workflow_path)]
        try:
            # Run the command and capture the output
            workspace = self._context_manager.get_context("workspace")
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True, cwd=str(workspace))
        
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while running the subprocess: {e.stderr} {e.stdout}")
            real_error = e.stderr+e.stdout
            if "data" in e.stdout.lower() or "handler" in e.stdout.lower():
                return [HyperparameterActionTask("Dataset", regenerate=True, error=real_error),
                        HyperparameterActionTask("DataHandler", regenerate=True, error=real_error),
                        ConfigActionTask("Dataset"), ConfigActionTask("DataHandler"), YamlEditTask("Dataset"),
                        YamlEditTask("DataHandler"), TrainTask()]
            elif "model" in e.stdout.lower():
                return [HyperparameterActionTask("Model", regenerate=True, error=real_error),
                        ConfigActionTask("Model"), YamlEditTask("Model"), TrainTask()]
            else:
                ret_list = []
                for component in COMPONENT_LIST:
                    ret_list.append(HyperparameterActionTask(component, regenerate=True, error=real_error))
                    ret_list.append(ConfigActionTask(component))
                    ret_list.append(YamlEditTask(component))
                ret_list.append(TrainTask())
                return ret_list

        return [AnalysisTask()]

    def summarize(self):
        if self._output is not None:
            # TODO: it will be overrides by later commands
            # utf8 can't decode normally on Windows
            self._context_manager.set_context(
                self.__class__.__name__, self._output.decode("ANSI")
            )


class AnalysisTask(Task):
    """
    This Recorder task is responsible for analysing data such as index and distribution.
    """

    __ANALYZERS_PROJECT = {
        HFAnalyzer.__name__: HFAnalyzer,
        SignalAnalyzer.__name__: SignalAnalyzer,
    }
    __ANALYZERS_DOCS = {
        HFAnalyzer.__name__: HFAnalyzer.__doc__,
        SignalAnalyzer.__name__: SignalAnalyzer.__doc__,
    }

    def __init__(self):
        super().__init__()

    def assign_context_manager(self, context_manager):
        # todo: add docstring to context temperature, perhaps store them in non runtime place is better.
        self._context_manager = context_manager
        for k, v in self.__ANALYZERS_DOCS.items():
            self._context_manager.set_context(k, v)

    def execute(self):
        prompt = self.user.render(
            user_prompt=self._context_manager.get_context("user_prompt")
        )
        be = APIBackend()
        be.debug_mode = False

        while True:
            response = be.build_messages_and_create_chat_completion(
                prompt,
                self.system.render(
                    ANALYZERS_list=list(self.__ANALYZERS_DOCS.keys()),
                    ANALYZERS_DOCS=self.__ANALYZERS_DOCS,
                ),
            )
            analysers = response.replace(" ", "").split(",")
            confirm = self.interact(f"I select these analysers: {analysers}\n"
                                    f"Are you sure you want to use? yes(Y/y), no(N/n) or prompt:")
            if confirm is False:
                analysers = []
                break
            elif confirm is True:
                break
            else:
                prompt = confirm

        if isinstance(analysers, list) and len(analysers):
            self.logger.info(f"selected analysers: {analysers}", plain=True)

            workflow_config = (
                self._context_manager.get_context("workflow_config")
                if self._context_manager.get_context("workflow_config")
                else "workflow_config.yaml"
            )
            workspace = self._context_manager.get_context("workspace")
            workflow_path = workspace.joinpath(workflow_config)
            with workflow_path.open() as f:
                workflow = yaml.safe_load(f)

            experiment_name = workflow["experiment_name"] if "experiment_name" in workflow else "workflow"
            R.set_uri(Path.joinpath(workspace, 'mlruns').as_uri())

            tasks = []
            for analyser in analysers:
                if analyser in self.__ANALYZERS_PROJECT.keys():
                    tasks.append(
                        self.__ANALYZERS_PROJECT.get(analyser)(
                            recorder=R.get_recorder(experiment_name=experiment_name),
                            output_dir=workspace
                        )
                    )

            for task in tasks:
                resp = task.analyse()
                self._context_manager.set_context(resp, task.__class__.__doc__)

        return []


class ActionTask(Task):
    pass


class CMDTask(ActionTask):
    """
    This CMD task is responsible for ensuring compatibility across different operating systems.
    """

    def __init__(self, cmd_intention: str, cwd=None):
        self.cwd = cwd
        self.cmd_intention = cmd_intention
        self._output = None
        super().__init__()

    def execute(self):
        prompt = self.user.render(
            cmd_intention=self.cmd_intention, user_os=platform.system()
        )
        response = APIBackend().build_messages_and_create_chat_completion(
            prompt, self.system.render()
        )
        self._output = subprocess.check_output(response, shell=True, cwd=self.cwd)
        return []

    def summarize(self):
        if self._output is not None:
            # TODO: it will be overrides by later commands
            # utf8 can't decode normally on Windows
            self._context_manager.set_context(
                self.__class__.__name__, self._output.decode("ANSI")
            )


class DifferentiatedComponentActionTask(ActionTask):
    @property
    def system(self):
        return self.prompt_template.__getattribute__(self.__class__.__name__ + "_system_" + self.target_component)

    @property
    def user(self):
        return self.prompt_template.__getattribute__(self.__class__.__name__ + "_user_" + self.target_component)


class HyperparameterActionTask(ActionTask):
    def __init__(self, component, regenerate=False, error=None, error_type=None) -> None:
        super().__init__()
        self.target_component = component
        self.regenerate = regenerate
        self.error = error
        self.error_type = error_type

    def execute(self):
        user_prompt = self._context_manager.get_context("user_prompt")

        target_component_decision = self._context_manager.get_context(f"{self.target_component}_decision")
        target_component_plan = self._context_manager.get_context(f"{self.target_component}_plan")
        target_component_classes = self._context_manager.get_context(f"{self.target_component}_classes")

        for module_path, class_name in target_component_classes:
            exec(f"from {module_path} import {class_name}")

        assert target_component_decision is not None, "target component decision is not set by plan maker"
        assert target_component_plan is not None, "target component plan is not set by plan maker"
        assert target_component_classes is not None, "target component classes is not set by plan maker"

        system_prompt = self.system.render(target_module=self.target_component, choice=target_component_decision, classes=target_component_classes)
        
        target_component_classes_and_hyperparameters = []
        for module_path, class_name in target_component_classes:
            exec(f"from {module_path} import {class_name}")
            hyperparameters = [hyperparameter for hyperparameter in {name: param for name, param in inspect.signature(eval(class_name).__init__).parameters.items() if name != "self" and name != "kwargs"}.keys()]
            if class_name == "LGBModel":
                hyperparameters.extend([ "boosting_type", "num_leaves", "max_depth", "learning_rate", "n_estimators", "objective", "class_weight", "min_split_gain", "min_child_weight", "min_child_samples", "subsample", "subsample_freq", "colsample_bytree", "reg_alpha", "reg_lambda", "random_state", "n_jobs", "silent", "importance_type", "early_stopping_round", "metric", "num_class", "is_unbalance", "bagging_seed", "verbosity", ])
            elif class_name == "SignalRecord":
                hyperparameters.remove("model")
                hyperparameters.remove("dataset")
                hyperparameters.remove("recorder")
            target_component_classes_and_hyperparameters.append((module_path, class_name, hyperparameters))
        user_prompt = self.user.render(
            user_requirement=user_prompt,
            target_component_plan=target_component_plan,
            target_component=self.target_component,
            target_component_classes_and_hyperparameters=target_component_classes_and_hyperparameters
        )
        former_messages = []
        if self.regenerate:
            if self.error_type == "yaml":
                user_prompt = f"your yaml config generated from your hyperparameter is not in the right format.\n The Yaml string generated from the hyperparameters is not in the right format.\nPlease rewrite the hyperparameters and answer with exact required format in system prompt and reply with no more explainations.\nThe error message: {self.error}. Please correct the former answer accordingly.\nHyperparameters, Reason and Improve suggestion should always be included."
            else:
                user_prompt = f"your hyperparameter cannot be initialized, may be caused by wrong format of the value or wrong name or some value is not supported in Qlib.\nPlease rewrite the hyperparameters and answer with exact required format in system prompt and reply with no more explainations.\nThe error message: {self.error}. Please correct the former answer accordingly.\nHyperparameters, Reason and Improve suggestion should always be included."
            former_messages = self._context_manager.get_context("chat_history")[self.__class__.__name__][self.target_component][1:]
        response = APIBackend().build_messages_and_create_chat_completion(
            user_prompt, system_prompt, former_messages=former_messages
        )
        self.save_chat_history_to_context_manager(
            user_prompt, response, system_prompt, self.target_component
        )
        res = re.search(
            r"(?i)Hyperparameters:(.*)Reason:(.*)Improve suggestion:(.*)", response, re.S
        )
        assert (
            res is not None and len(res.groups()) == 3
        ), "The response of config action task is not in the correct format"

        hyperparameters = res.group(1)
        reason = res.group(2)
        improve_suggestion = res.group(3)

        self._context_manager.set_context(f"{self.target_component}_hyperparameters", hyperparameters)
        self._context_manager.set_context(f"{self.target_component}_reason", reason)
        self._context_manager.set_context(
            f"{self.target_component}_improve_suggestion", improve_suggestion
        )
        return []


class ConfigActionTask(ActionTask):
    def __init__(self, component) -> None:
        super().__init__()
        self.target_component = component
    
    def execute(self):
        user_prompt = self._context_manager.get_context("user_prompt")

        target_component_decision = self._context_manager.get_context(f"{self.target_component}_decision")
        target_component_plan = self._context_manager.get_context(f"{self.target_component}_plan")
        target_component_classes = self._context_manager.get_context(f"{self.target_component}_classes")
        target_component_hyperparameters = self._context_manager.get_context(f"{self.target_component}_hyperparameters")

        system_prompt = self.system.render(target_module=self.target_component, choice=target_component_decision, classes=target_component_classes)
        user_prompt = self.user.render(
            user_requirement=user_prompt,
            target_component_plan=target_component_plan,
            target_component=self.target_component,
            target_component_hyperparameters=target_component_hyperparameters
        )
        former_messages = []
        # if self.reconfig and user_prompt == self._context_manager.get_context("chat_history")[self.__class__.__name__][self.target_component][-2]["content"]:
        #     user_prompt = f"your config cannot be converted to YAML, may be caused by wrong format. Please rewrite the yaml and answer with exact required format in system prompt and reply with no more explainations.\nerror message: {self.error}\n"
        #     former_messages = self._context_manager.get_context("chat_history")[self.__class__.__name__][self.target_component][1:]
        response = APIBackend().build_messages_and_create_chat_completion(
            user_prompt, system_prompt, former_messages=former_messages
        )
        self.save_chat_history_to_context_manager(
            user_prompt, response, system_prompt, self.target_component
        )
        config = re.search(r"```yaml(.*)```", response, re.S).group(1)

        try:
            yaml_config = yaml.safe_load(io.StringIO(config))
        except yaml.YAMLError as e:
            self.logger.info(f"Yaml file is not in the correct format: {e}")
            return_tasks = [HyperparameterActionTask(self.target_component, regenerate=True, error=str(e), error_type="yaml"),  ConfigActionTask(self.target_component)]
            return return_tasks
        
        if self.target_component == "Dataset":
            if 'handler' in yaml_config["dataset"]:
                del yaml_config['dataset']['handler']
        elif self.target_component == "DataHandler":
            for processor in yaml_config['handler']['kwargs']['infer_processors']:
                if "kwargs" in processor and "fields_group" in processor["kwargs"]:
                    del processor["kwargs"]['fields_group']
            for processor in yaml_config['handler']['kwargs']['learn_processors']:
                if "kwargs" in processor and "fields_group" in processor["kwargs"]:
                    del processor["kwargs"]['fields_group']
            
            if 'freq' in yaml_config['handler']['kwargs']:
                yaml_config['handler']['kwargs']['freq'] = "day" # TODO hot fix freq because no data
        elif self.target_component == "Record":
            for record in yaml_config['record']:
                if record['class'] == 'SigAnaRecord' and 'label_col' in record['kwargs']:
                    del record['kwargs']["label_col"]
        
        def remove_default(config):
            if isinstance(config, dict):
                for key in list(config.keys()):
                    if isinstance(config[key], str):
                        if config[key].lower() == "default":
                            del config[key]
                    else:
                        remove_default(config[key])
            elif isinstance(config, list):
                for item in config:
                    remove_default(item)
        remove_default(yaml_config)

        self._context_manager.set_context(f"{self.target_component}_config", yaml_config)
        return []

    


class ImplementActionTask(DifferentiatedComponentActionTask):
    def __init__(self, target_component, reimplement=False) -> None:
        super().__init__()
        self.target_component = target_component
        assert COMPONENT_LIST.index(self.target_component) <= 2, "The target component is not in dataset datahandler and model"
        self.reimplement = reimplement

    def execute(self):
        """
        return a list of interested tasks
        Copy the template project maybe a part of the task
        """

        user_prompt = self._context_manager.get_context("user_prompt")
        prompt_element_dict = dict()
        for component in COMPONENT_LIST:
            prompt_element_dict[
                f"{component}_decision"
            ] = self._context_manager.get_context(f"{component}_decision")
            prompt_element_dict[
                f"{component}_plan"
            ] = self._context_manager.get_context(f"{component}_plan")

        assert (
            None not in prompt_element_dict.values()
        ), "Some decision or plan is not set by plan maker"
        config = self._context_manager.get_context(f"{self.target_component}_config")

        implement_prompt = self.user.render(
            user_requirement=user_prompt,
            decision=prompt_element_dict[f"{self.target_component}_decision"],
            plan=prompt_element_dict[f"{self.target_component}_plan"],
            user_config=config,
        )
        former_messages = []
        if self.reimplement:
            implement_prompt = "your code seems wrong, please re-implement it and answer with exact required format and reply with no more explainations.\n"
            former_messages = self._context_manager.get_context("chat_history")[self.__class__.__name__][self.target_component][1:]
        response = APIBackend().build_messages_and_create_chat_completion(
            implement_prompt, self.system.render(), former_messages=former_messages
        )
        self.save_chat_history_to_context_manager(
            implement_prompt, response, self.system.render(), self.target_component
        )

        res = re.search(
            r"Code:(.*)Explanation:(.*)Modified config:(.*)", response, re.S
        )
        assert (
            res is not None and len(res.groups()) == 3
        ), f"The response of implement action task of component {self.target_component} is not in the correct format"

        code = re.search(r"```python(.*)```", res.group(1), re.S)
        assert (
            code is not None
        ), "The code part of implementation action task response is not in the correct format"
        code = code.group(1)
        explanation = res.group(2)
        modified_config = re.search(r"```yaml(.*)```", res.group(3), re.S)
        assert (
            modified_config is not None
        ), "The modified config part of implementation action task response is not in the correct format"
        modified_config = modified_config.group(1)

        self._context_manager.set_context(f"{self.target_component}_code", code)
        self._context_manager.set_context(
            f"{self.target_component}_code_explanation", explanation
        )
        self._context_manager.set_context(
            f"{self.target_component}_modified_config", modified_config
        )

        return []


class YamlEditTask(ActionTask):
    """This yaml edit task will replace a specific component directly"""

    def __init__(self, target_component: str):
        """

        Parameters
        ----------
        file
            a target file that needs to be modified
        module_path
            the path to the section that needs to be replaced with `updated_content`
        updated_content
            The content to replace the original content in `module_path`
        """
        super().__init__()
        self.target_component = target_component
        self.target_config_key = {
            "Dataset": "dataset",
            "DataHandler": "handler",
            "Model": "model",
            "Strategy": "strategy",
            "Record": "record",
            "Backtest": "backtest",
        }[self.target_component]
    
    def replace_key_value_recursive(self, target_dict, target_key, new_value):
        res = False
        if isinstance(target_dict, dict):  
            for key, value in target_dict.items():  
                if key == target_key:  
                    target_dict[key] = new_value
                    res = True
                else:  
                    res = res | self.replace_key_value_recursive(value, target_key, new_value)  
        elif isinstance(target_dict, list):  
            for item in target_dict:  
                res = res | self.replace_key_value_recursive(item, target_key, new_value) 
        return res


    def execute(self):
        # 1) read original and new content
        self.original_config_location = Path(os.path.join(self._context_manager.get_context('workspace'), "workflow_config.yaml"))
        with self.original_config_location.open("r") as f:
            target_config = yaml.safe_load(f)
        update_config = self._context_manager.get_context(f'{self.target_component}_modified_config')
        if update_config is None:
            update_config = self._context_manager.get_context(f'{self.target_component}_config')
            
        # 2) modify the module_path if code is implemented by finco
        # TODO because we skip code writing part, so we mute this step to avoid error
        # if self._context_manager.get_context(f'{self.target_component}_decision') == "Personized":
        #     workspace = self._context_manager.get_context(f'workspace').name
        #     module_path = f"qlib.finco.{workspace}.{self.target_component}_code"
        #     if "module_path" in update_config[self.target_config_key]:
        #         update_config[self.target_config_key]["module_path"] = module_path

        # TODO here's small trick, we only update dataset and datahandler in the whole, so we skip dataset update and update both of them when datahandler is set. Because model may not set datahandler in dataset config, then we may not find 'handler' in the new config, this trick can be updated in the future.
        if self.target_component == "Dataset":
            return []
        elif self.target_component == "DataHandler":
            dataset_update_config = self._context_manager.get_context(f'Dataset_modified_config')
            if dataset_update_config is None:
                dataset_update_config = self._context_manager.get_context(f'Dataset_config')
            dataset_update_config['dataset']['kwargs']['handler'] = update_config['handler']
            update_config = dataset_update_config
            real_target_config_key = "dataset"
        else:
            real_target_config_key = self.target_config_key

        # 3) replace the module
        assert isinstance(update_config, dict) and real_target_config_key in update_config, "The config file is not in the correct format"
        assert self.replace_key_value_recursive(target_config, real_target_config_key, update_config[real_target_config_key]), "Replace of the yaml file failed."

        # TODO hotfix for the bug that the record signalrecord config is not updated
        for record in target_config['task']['record']:
            if record['class'] == 'SignalRecord':
                if 'kwargs' in record and 'model' in record['kwargs']:
                    del record['kwargs']["model"]
                if 'kwargs' in record and 'dataset' in record['kwargs']:
                    del record['kwargs']["dataset"]
        
        # 4) save the config file
        with self.original_config_location.open("w") as f:
            yaml.dump(target_config, f)

        return []

class CodeDumpTask(ActionTask):
    def __init__(self, target_component) -> None:
        super().__init__()
        self.target_component = target_component
    
    def execute(self):
        code = self._context_manager.get_context(f'{self.target_component}_code')
        assert code is not None, "The code is not set"
        
        with open(os.path.join(self._context_manager.get_context('workspace'), f'{self.target_component}_code.py'), 'w') as f:
            f.write(code)
        
        try:
            exec(f"from qlib.finco.{os.path.basename(self._context_manager.get_context('workspace'))}.{self.target_component}_code import *")
        except (ImportError, AttributeError, SyntaxError):
            return [ImplementActionTask(self.target_component, reimplement=True), CodeDumpTask(self.target_component)]
        return []


class SummarizeTask(Task):
    __DEFAULT_SUMMARIZE_CONTEXT = ["workflow_yaml", "metrics"]

    # TODO: 2048 is close to exceed GPT token limit
    __MAX_LENGTH_OF_FILE = 2048
    __DEFAULT_REPORT_NAME = "finCoReport.md"

    def __init__(self):
        super().__init__()
        self.workspace = "./"

    @property
    def summarize_context_system(self):
        return self.prompt_template.get(self.__class__.__name__ + "_context_system")

    @property
    def summarize_context_user(self):
        return self.prompt_template.get(self.__class__.__name__ + "_context_user")

    @property
    def summarize_metrics_system(self):
        return self.prompt_template.get(self.__class__.__name__ + "_metrics_system")

    @property
    def summarize_metrics_user(self):
        return self.prompt_template.get(self.__class__.__name__ + "_metrics_user")

    def execute(self) -> Any:
        workspace = self._context_manager.get_context("workspace")
        user_prompt = self._context_manager.get_context("user_prompt")
        workflow_yaml = self._context_manager.get_context("workflow_yaml")

        file_info = self.get_info_from_file(workspace)
        context_info = self.get_info_from_context()  # too long context make response unstable.
        record_info = self.get_info_from_recorder(workspace, workflow_yaml["experiment_name"])
        figure_path = self.get_figure_path(workspace)

        information = context_info + file_info + record_info

        def _get_value_from_info(info: list, k: str):
            for i in information:
                if k in i.keys():
                    return i.get(k)
            return ""

        # todo: remove 'be' after test
        be = APIBackend()
        be.debug_mode = False

        context_summary = {}
        for key in self.__DEFAULT_SUMMARIZE_CONTEXT:
            prompt_workflow_selection = self.summarize_context_user.render(
                key=key, value=_get_value_from_info(info=information, k=key)
            )
            response = be.build_messages_and_create_chat_completion(
                user_prompt=prompt_workflow_selection, system_prompt=self.summarize_context_system.render()
            )
            context_summary.update({key: response})

        recorder = R.get_recorder(experiment_name=workflow_yaml["experiment_name"])
        recorder.save_objects(context_summary=context_summary)

        prompt_workflow_selection = self.summarize_metrics_user.render(
            information=_get_value_from_info(info=record_info, k="metrics"), user_prompt=user_prompt
        )
        metrics_response = be.build_messages_and_create_chat_completion(
            user_prompt=prompt_workflow_selection, system_prompt=self.summarize_metrics_system.render()
        )

        prompt_workflow_selection = self.user.render(
            information=file_info + [{"metrics": metrics_response}], figure_path=figure_path, user_prompt=user_prompt
        )
        response = be.build_messages_and_create_chat_completion(
            user_prompt=prompt_workflow_selection, system_prompt=self.system.render()
        )

        self._context_manager.set_context("summary", response)
        self.save_markdown(content=response, path=workspace)
        self.logger.info(f"Report has saved to {self.__DEFAULT_REPORT_NAME}", title="End")

        return []

    def summarize(self) -> str:
        return ""

    def get_info_from_file(self, path) -> List:
        """
        read specific type of files under path
        """
        file_list = []
        path = Path.cwd().joinpath(path).resolve()
        for root, dirs, files in os.walk(path):
            for filename in files:
                file_path = os.path.join(root, filename)
                file_list.append(Path(file_path))

        result = []
        for file in file_list:
            postfix = file.name.split(".")[-1]
            if postfix in ["py", "log", "yaml"]:
                with open(file) as f:
                    content = f.read()
                    self.logger.info(f"file to summarize: {file}", plain=True)
                    # in case of too large file
                    # TODO: Perhaps summarization method instead of truncation would be a better approach
                    result.append(
                        {"file": file.name, "content": content[: self.__MAX_LENGTH_OF_FILE],
                         "additional": self._context_manager.retrieve(file.name)}
                    )

        return result

    def get_info_from_context(self):
        context = []
        for key, v in self._context_manager.context.items():
            if v is not None:
                v = str(v)
                context.append({key: v[: self.__MAX_LENGTH_OF_FILE]})
        return context

    @staticmethod
    def get_info_from_recorder(path, exp_name) -> list:
        path = Path(path)
        path = path if path.name == "mlruns" else path.joinpath("mlruns")

        R.set_uri(Path(path).as_uri())
        exp = R.get_exp(experiment_name=exp_name)

        records = []
        recorders = exp.list_recorders(rtype=exp.RT_L)
        if len(recorders) == 0:
            return records

        # get info from the latest recorder, sort by end time is considerable
        recorders = sorted(recorders, key=lambda x: x.experiment_id)
        recorder = recorders[-1]

        records.append({"metrics": recorder.list_metrics()})
        return records

    def get_figure_path(self, path):
        file_list = []

        for root, dirs, files in os.walk(Path(path)):
            for filename in files:
                postfix = filename.split(".")[-1]
                if postfix in ["jpeg"]:
                    description = self._context_manager.retrieve(filename)
                    file_list.append({"file_name": filename, "description": description,
                                      "path": str(Path(path).joinpath(filename))})
        return file_list

    def save_markdown(self, content: str, path):
        with open(Path(path).joinpath(self.__DEFAULT_REPORT_NAME), "w") as f:
            f.write(content)
