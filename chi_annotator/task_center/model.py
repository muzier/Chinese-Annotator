import datetime
import io
import json
import logging
import os
import copy


from chi_annotator.task_center.components import ComponentBuilder
from chi_annotator.task_center.components import Component
from chi_annotator.task_center.common import Metadata
from chi_annotator.taks_center.common import Message
from chi_annotator.task_center.utils import *

logger = logging.getLogger(__name__)

class Trainer(object):
    """Trainer will load the data and train all components.

    Requires a pipeline specification and configuration to use for
    the training."""

    # Officially supported languages (others might be used, but might fail)
    SUPPORTED_LANGUAGES = ["zh"]

    def __init__(self, config, component_builder=None, skip_validation=False):
        # type: (RasaNLUConfig, Optional[ComponentBuilder], bool) -> None

        self.config = config
        self.skip_validation = skip_validation
        self.training_data = None  # type: Optional[TrainingData]
        self.pipeline = []  # type: List[Component]
        if component_builder is None:
            # If no builder is passed, every interpreter creation will result in
            # a new builder. hence, no components are reused.
            component_builder = components.ComponentBuilder()

        # Before instantiating the component classes, lets check if all
        # required packages are available
        # TODO
        #if not self.skip_validation:
        #    components.validate_requirements(config.pipeline)

        # Transform the passed names of the pipeline components into classes
        for component_name in config.pipeline:
            component = component_builder.create_component(
                    component_name, config)
            self.pipeline.append(component)

    def train(self, data):
        # type: (TrainingData) -> Interpreter
        """Trains the underlying pipeline using the provided training data."""

        self.training_data = data

        context = {}  # type: Dict[Text, Any]

        for component in self.pipeline:
            updates = component.provide_context()
            if updates:
                context.update(updates)

        # Before the training starts: check that all arguments are provided
        if not self.skip_validation:
            components.validate_arguments(self.pipeline, context)

        # data gets modified internally during the training - hence the copy
        working_data = copy.deepcopy(data)

        for i, component in enumerate(self.pipeline):
            logger.info("Starting to train component {}".format(component.name))
            component.prepare_partial_processing(self.pipeline[:i], context)
            updates = component.train(working_data, self.config, **context)
            logger.info("Finished training component.")
            if updates:
                context.update(updates)

        return Interpreter(self.pipeline, context)

    def persist(self, path, persistor=None, project_name=None,
                fixed_model_name=None):
        # type: (Text, Optional[Persistor], Text) -> Text
        """Persist all components of the pipeline to the passed path.

        Returns the directory of the persisted model."""

        timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        metadata = {
            "language": self.config["language"],
            "pipeline": [utils.module_path_from_object(component)
                         for component in self.pipeline],
        }

        if project_name is None:
            project_name = "default"

        if fixed_model_name:
            model_name = fixed_model_name
        else:
            model_name = "model_" + timestamp
        dir_name = os.path.join(path, project_name, model_name)

        create_dir(dir_name)

        if self.training_data:
            metadata.update(self.training_data.persist(dir_name))

        for component in self.pipeline:
            update = component.persist(dir_name)
            if update:
                metadata.update(update)

        Metadata(metadata, dir_name).persist(dir_name)

        if persistor is not None:
            persistor.persist(dir_name, model_name, project_name)
        logger.info("Successfully saved model into "
                    "'{}'".format(os.path.abspath(dir_name)))
        return dir_name


class Interpreter(object):
    """Use a trained pipeline of components to parse text messages"""

    # Defines all attributes (& default values) that will be returned by `parse`
    @staticmethod
    def default_output_attributes():
        return {"intent": {"name": "", "confidence": 0.0}, "entities": []}

    @staticmethod
    def load(model_dir, config=RasaNLUConfig(), component_builder=None,
             skip_valdation=False):
        """Creates an interpreter based on a persisted model."""

        if isinstance(model_dir, Metadata):
            # this is for backwards compatibilities (metadata passed as a dict)
            model_metadata = model_dir
            logger.warn("Deprecated use of `Interpreter.load` with a metadata "
                        "object. If you want to directly pass the metadata, "
                        "use `Interpreter.create(metadata, ...)`. If you want "
                        "to load the metadata from file, use "
                        "`Interpreter.load(model_dir, ...)")
        else:
            model_metadata = Metadata.load(model_dir)
        return Interpreter.create(model_metadata, config, component_builder,
                                  skip_valdation)

    @staticmethod
    def create(model_metadata,  # type: Metadata
               config,  # type: RasaNLUConfig
               component_builder=None,  # type: Optional[ComponentBuilder]
               skip_valdation=False  # type: bool
               ):
        # type: (...) -> Interpreter
        """Load stored model and components defined by the provided metadata."""

        context = {}

        if component_builder is None:
            # If no builder is passed, every interpreter creation will result
            # in a new builder. hence, no components are reused.
            component_builder = components.ComponentBuilder()

        pipeline = []

        # Before instantiating the component classes,
        # lets check if all required packages are available
        if not skip_valdation:
            components.validate_requirements(model_metadata.pipeline)

        for component_name in model_metadata.pipeline:
            component = component_builder.load_component(
                    component_name, model_metadata.model_dir,
                    model_metadata, config=config, **context)
            try:
                updates = component.provide_context()
                if updates:
                    context.update(updates)
                pipeline.append(component)
            except components.MissingArgumentError as e:
                raise Exception("Failed to initialize component '{}'. "
                                "{}".format(component.name, e))

        return Interpreter(pipeline, context, model_metadata)

    def __init__(self, pipeline, context, model_metadata=None):
        # type: (List[Component], Dict[Text, Any], Optional[Metadata]) -> None

        self.pipeline = pipeline
        self.context = context if context is not None else {}
        self.model_metadata = model_metadata

    def parse(self, text, time=None):
        # type: (Text) -> Dict[Text, Any]
        """Parse the input text, classify it and return pipeline result.

        The pipeline result usually contains intent and entities."""

        if not text:
            # Not all components are able to handle empty strings. So we need
            # to prevent that... This default return will not contain all
            # output attributes of all components, but in the end, no one should
            # pass an empty string in the first place.
            output = self.default_output_attributes()
            output["text"] = ""
            return output

        message = Message(text, self.default_output_attributes(), time=time)

        for component in self.pipeline:
            component.process(message, **self.context)

        output = self.default_output_attributes()
        output.update(message.as_dict(only_output_properties=True))
        return output
