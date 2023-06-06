import typing
from typing import (
    List,
    Any,
    Dict,
    Union,
    Literal,
    Tuple,
    Type,
    Generator,
    get_type_hints,
    Optional,
    Mapping,
)
import inspect
from functools import singledispatch
from pydantic import BaseModel, fields, BaseSettings
from pathlib import Path, PurePath
import yaml


def get_nested_dict_attr_by_path(
    data: Dict, key_path: List[str], fallback_val: Any = Any
) -> Any:
    """Provide multiple dict keys as a list to acces a nested dict attribute.

    Args:
        data (Dict): _description_
        keys (List[str]): _description_
        default (Any, optional): _description_. Defaults to Any.

    Returns:
        Any: _description_
    """

    # i know. the `fallback_val` solution with Any as default value to emulate an optional parameter is hacky. Overloading in python is hard :)
    try:
        # thanks to https://stackoverflow.com/a/47969823/12438690
        return (
            get_nested_dict_attr_by_path(data[key_path[0]], key_path[1:], fallback_val)
            if key_path
            else data
        )
    except KeyError:
        if fallback_val != Any:
            return fallback_val
        else:
            raise


def create_nested_dict_by_path(
    path: str, value: Any, path_seperator: str = "."
) -> Dict:
    """_summary_

    Args:
        path (str): _description_
        value (Any): _description_

    Returns:
        Dict: _description_
    """
    keys = path.split(path_seperator)
    nested_dict = {}
    current_dict = nested_dict

    for key in keys[:-1]:
        current_dict[key] = {}
        current_dict = current_dict[key]

    current_dict[keys[-1]] = value

    return nested_dict


#######################
##### experiments #####
#######################


class YamlConfigFileHandler:
    def __init__(self, model: Type[BaseSettings], file_path: Union[str, Path] = None):
        self.config_file: Path = (
            file_path if isinstance(file_path, Path) else Path(file_path)
        )
        self.model = model

    def generate_example_config_file(self):
        config = self.model.parse_obj(
            self._get_fields_filler(
                required_only=False,
                use_example_values_if_exists=True,
            )
        )
        self._generate_file(config, generate_with_example_values=True)

    def generate_existing_config_file(self, config: BaseSettings):
        self._generate_file(config)

    def generate_minimal_config_file(self):
        config = self.model.parse_obj(
            self._get_fields_filler(
                required_only=True,
                use_example_values_if_exists=True,
            )
        )
        self._generate_file(config, generate_with_optional_fields=False)

    def generate_markdown_doc(self):
        pass

    def _generate_file(
        self,
        config: BaseSettings,
        overwrite_existing: bool = False,
        generate_with_optional_fields: bool = True,
        comment_out_optional_fields: bool = True,
        generate_with_comment_desc_header: bool = True,
        generate_with_example_values: bool = False,
    ):
        self.config_file.parent.mkdir(exist_ok=True, parents=True)
        if self.config_file.is_file() and not overwrite_existing:
            raise FileExistsError(
                f"Can not generate config file at {self.config_file}. File allready exists."
            )
        yaml_content: str = yaml.dump(config.dict())
        yaml_content_with_comment: List[str] = []
        previous_depth = 0
        previous_key: str = None
        current_path: List[str] = []
        in_multiline_block: bool = False
        for line in yaml_content.split("\n"):
            line_no_indent = line.lstrip()

            depth = int((len(line) - len(line_no_indent)) / 2)
            if previous_depth < depth:
                current_path.append(previous_key)
            elif depth < previous_depth:
                for i in range(depth, previous_depth):
                    current_path.pop()
            field = None
            if line_no_indent.startswith("- "):
                # we are in list element
                pass

            elif ": " in line:
                key, val = line.split(": ")
                key = key.strip()
                # field = self._get_field_info(key, current_path)
                yaml_content_with_comment.append(
                    self.generate_field_header(self._get_field_info(key, current_path))
                )
                previous_key = key
            elif line.endswith(":"):
                # sub chapter or line start
                previous_key = line.split(":")[0]
            previous_depth = depth
            yaml_content_with_comment.append(line)
        for line in yaml_content_with_comment:
            print(line)

    def _get_field_info(self, key: str, path: List[str]) -> fields.ModelField:
        current_obj = self.model
        print(path + [key])
        for parent_key in path + [key]:
            if isinstance(current_obj, fields.ModelField):
                current_obj = current_obj.type_.__fields__[parent_key]
            else:
                current_obj = current_obj.__fields__[parent_key]
        return current_obj  # if isinstance(current_obj, fields.ModelField) else None

    def generate_field_header(self, field_meta_data: fields.ModelField):
        return f"# {field_meta_data}\n# BLABLA"

    def _get_fields_filler(
        self,
        required_only: bool = True,
        use_example_values_if_exists: bool = False,
        fallback_fill_value: Any = None,
    ) -> Dict:
        """Needed for creating dummy values for non nullable values. Otherwise we are not able to initialize a living config from the model

        Args:
            required_only (bool, optional): _description_. Defaults to True.
            use_example_values_if_exists (bool, optional): _description_. Defaults to False.
            fallback_fill_value (Any, optional): _description_. Defaults to None.

        Returns:
            Dict: _description_
        """

        def parse_model_class(m_cls: Type[BaseModel]) -> Dict:
            result: Dict = {}
            for key, field in m_cls.__fields__.items():
                if not required_only or field.required:
                    if inspect.isclass(field.annotation) and issubclass(
                        field.annotation, BaseModel
                    ):
                        result[key] = parse_model_class(field.type_)
                    elif (
                        use_example_values_if_exists
                        and "example" in field.field_info.extra
                    ):
                        result[key] = field.field_info.extra["example"]
                    else:
                        result[key] = (
                            fallback_fill_value if field.required else field.default
                        )
            return result

        return parse_model_class(self.model)


class ConfigValueNotSet:
    pass


class ConfigFieldProps:
    def __init__(
        self,
        name: str,
        pydantic_schema_item_properties: Dict,
        type_hint,
        parent_config_chapter: "ConfigChapterMetaContainer" = None,
        set_value: Any = ConfigValueNotSet,
    ):
        self.name = name
        self._pydantic_schema_item_properties = pydantic_schema_item_properties
        self.title = None
        if "title" in pydantic_schema_item_properties:
            self.title = pydantic_schema_item_properties["title"]

        self.description = None
        if "description" in pydantic_schema_item_properties:
            self.description = pydantic_schema_item_properties["description"]

        self.example = None
        if "example" in pydantic_schema_item_properties:
            self.example = pydantic_schema_item_properties["example"]

        self.alias = None
        if "alias" in pydantic_schema_item_properties:
            self.alias = pydantic_schema_item_properties["alias"]

        self.type = None
        if "type" in pydantic_schema_item_properties:
            self.type = pydantic_schema_item_properties["type"]

        self.enum = None
        if "enum" in pydantic_schema_item_properties:
            self.enum = pydantic_schema_item_properties["enum"]

        if "default" in pydantic_schema_item_properties:
            self.default = pydantic_schema_item_properties["default"]
        self.parent_config_chapter = parent_config_chapter
        self.type_hint = type_hint
        self.set_value = set_value

    @property
    def value(self):
        if self.set_value == ConfigValueNotSet and not self.has_default():
            raise ValueError(
                f"No value for {self.parent_config_chapter.get_path_as_str()}"
            )
        elif self.set_value != ConfigValueNotSet:
            return self.set_value
        else:
            return self.default

    def get_env_name(self, prefix: str):
        env_name = "_".join(
            [
                "".join(c for c in path_node if c.isalnum())
                for path_node in self.parent_config_chapter.get_path()
            ]
        ).upper()
        return f"{prefix.upper()}_{env_name}" if prefix else env_name

    def has_default(self):
        return hasattr(self, "default")

    def has_to_be_set(self):
        return not self.has_default()


class ConfigChapterMetaContainer:
    def __init__(
        self,
        config_object: Any,
        parent: "ConfigChapterMetaContainer" = None,
        parent_attr: str = None,
        parent_key: str = None,
        parent_index: int = None,
        field_props: ConfigFieldProps = None,
        depth: int = 0,
    ):
        self.config_object = config_object
        self.parent_container = parent
        self.parent_attr = parent_attr
        self.parent_key = parent_key
        self.parent_index = parent_index
        self.field_props = field_props
        if field_props is not None:
            self.field_props.parent_config_chapter = self
        self.depth = depth

    def get_path(self) -> List[Union[str, int]]:
        path = []
        if self.parent_container:
            path.extend(self.parent_container.get_path())
        if self.get_name():
            path.append(self.get_name())
        return path

    def get_path_as_str(self, seperator: str = ".") -> str:
        return seperator.join(self.get_path())

    def is_value_field(self) -> bool:
        return isinstance(self.field_props, ConfigFieldProps)

    def get_name(self):
        return self.parent_attr or self.parent_key or self.parent_index

    def is_optional(self):
        if self.is_value_field():
            return self.field_props.has_to_be_set()
        else:
            for child_obj in ConfigHandler(self.config_object)._walk_config(
                self.config_object, max_depth=1
            ):
                child_path = child_obj.get_path()
                if self.parent_container:
                    child_path = self.parent_container.get_path() + child_path

                if len(child_path) > len(self.get_path()):
                    if not child_obj.is_optional():
                        return False


class ConfigHandler:
    def __init__(
        self,
        config_model_class: Type[BaseModel],
        config_file: Union[str, Path, None] = None,
        environment_var_prefix: str = None,
    ):
        self.config_model_class = config_model_class
        self.config_file = None
        if config_file:
            self.config_file: Path = (
                config_file if isinstance(config_file, Path) else Path(config_file)
            )
        self.environment_var_prefix: str = (
            environment_var_prefix if environment_var_prefix else ""
        )
        self._resulting_config: BaseModel = None

    def get_fields_filler(
        self,
        config_model_class: Type[BaseModel] = None,
        required_only: bool = True,
        use_example_values_if_exists: bool = False,
        fallback_fill_value: Any = None,
    ) -> Dict:
        def parse_model_class(m_cls: Type[BaseModel]) -> Dict:
            result: Dict = {}
            for key, field in m_cls.__fields__.items():
                if not required_only or field.required:
                    if inspect.isclass(field.annotation) and issubclass(
                        field.annotation, BaseModel
                    ):
                        result[key] = parse_model_class(field.type_)
                    elif (
                        use_example_values_if_exists
                        and "example" in field.field_info.extra
                    ):
                        result[key] = field.field_info.extra["example"]
                    else:
                        result[key] = (
                            fallback_fill_value if field.required else field.default
                        )
            return result

        if config_model_class is None:
            config_model_class = self.config_model_class
        return parse_model_class(config_model_class)

    def load_config(self, reload: bool = False):
        """_summary_

        Args:
            reload (bool, optional): If set to True; reload data from config file and env variables. Defaults to False.
        """
        if self._resulting_config is None or reload:
            # load default config
            # self._resulting_config = self.config_model_class.construct()
            config_values: Dict = {}
            # load file to override default config
            if self.config_file:
                with open(self.config_file, "r") as stream:
                    config_values = yaml.safe_load(stream)
            config_values = config_values | self.load_config_from_environment_var(
                self._resulting_config
            )

            # merge config from file onto default config
            self._resulting_config = self.config_model_class.parse_obj(config_values)

        for obj in self._walk_config(self._resulting_config):
            print("#" * (obj.depth + 1))

            print(
                type(obj),
                obj.get_path(),
                obj.field_props.name if obj.is_value_field() else None,
            )

            if obj.is_value_field():
                print("####field")
                print(obj.field_props.get_env_name(self.environment_var_prefix))
                print(obj.field_props._pydantic_schema_item_properties)
                print(obj.field_props.type_hint)
                print("SET_VALUE", obj.field_props.set_value)
                print(
                    "DEF_VALUE",
                    obj.field_props.default if obj.field_props.has_default() else "",
                )
                print("VALUE", obj.field_props.value)

    def load_config_from_environment_var(self, config: BaseModel) -> Dict:
        values: Dict = {}
        for obj in self._walk_config(config):
            if obj.is_value_field():
                environment_var_name = obj.field_props.get_env_name(
                    self.environment_var_prefix
                )
                if environment_var_name in os.environ:
                    value = os.getenv(environment_var_name)
                    values = values | create_nested_dict_by_path(
                        path=obj.get_path_as_str(), value=value
                    )
        return values

    def _walk_config(
        self,
        config_obj: Union[BaseModel, List, Dict],
        parent: ConfigChapterMetaContainer = None,
        parent_attr: str = None,
        parent_key: str = None,
        parent_index: int = None,
        field_props: ConfigFieldProps = None,
        depth: int = 0,
        max_depth: int = None,
    ) -> Generator[ConfigChapterMetaContainer, None, None]:
        if max_depth and depth > max_depth:
            return
        if isinstance(config_obj, (int, str, float, bool, complex, type(None))):
            return
        this = ConfigChapterMetaContainer(
            config_obj,
            parent,
            parent_attr=parent_attr,
            parent_key=parent_key,
            parent_index=parent_index,
            field_props=field_props,
            depth=depth,
        )
        yield this
        if isinstance(config_obj, list):
            for index, child_obj in enumerate(config_obj):
                yield from self._walk_config(
                    child_obj, parent=this, parent_index=index, depth=depth + 1
                )
        elif isinstance(config_obj, dict):
            for key, child_obj in config_obj.items():
                yield from self._walk_config(
                    child_obj, parent=this, parent_key=key, depth=depth + 1
                )

        elif isinstance(config_obj, BaseModel):
            pydantic_field_props: Dict = this.config_object.schema()["properties"]
            type_hints = get_type_hints(config_obj)
            set_values = config_obj.dict(exclude_unset=True, exclude_defaults=True)
            for attr, child_obj in config_obj._iter():
                field_prop = None
                if (
                    "type" in pydantic_field_props[attr]
                    and pydantic_field_props[attr]["type"] != "object"
                ):
                    set_value = ConfigValueNotSet
                    if attr in set_values:
                        set_value = set_values[attr]

                    field_prop = ConfigFieldProps(
                        name=attr,
                        pydantic_schema_item_properties=pydantic_field_props[attr],
                        type_hint=type_hints[attr],
                        parent_config_chapter=this,
                        set_value=set_value,
                    )

                yield from self._walk_config(
                    child_obj,
                    parent=this,
                    parent_attr=attr,
                    field_props=field_prop,
                    depth=depth + 1,
                )

    def get_config(self) -> BaseModel:
        return self._resulting_config

    def generate_config_file(
        self,
        config_obj: Union[BaseModel, List, Dict] = None,
        target_path: Union[str, Path] = None,
        overwrite_existing: bool = False,
        generate_with_optional_fields: bool = True,
        comment_out_optional_fields: bool = True,
        generate_with_comment_desc_header: bool = True,
        generate_with_example_values: bool = True,
        format: Literal["yaml"] = "yaml",
    ):
        if config_obj is None:
            config_obj = self.config_model_class.parse_obj(
                self.get_fields_filler(
                    config_model_class=self.config_model_class,
                    required_only=not generate_with_example_values,
                    use_example_values_if_exists=generate_with_example_values,
                )
            )
        target_path: Path = (
            self.config_file
            if target_path is None
            else Path(target_path)
            if isinstance(target_path, str)
            else target_path
        )
        target_path.parent.mkdir(exist_ok=True, parents=True)
        if target_path.is_file() and not overwrite_existing:
            raise FileExistsError(
                f"Can not generate config file at {target_path}. File allready exists."
            )
        file_content: List[str] = []
        for obj in self._walk_config(config_obj):
            # typing help
            obj: ConfigChapterMetaContainer = obj
            is_optional = obj.is_optional()
            line_prefix = "  " * obj.depth
            if is_optional and generate_with_optional_fields:
                line_prefix += "#" if comment_out_optional_fields else ""
            elif is_optional and not generate_with_optional_fields:
                continue

            comment_section: str = None
            line = f"{obj.get_name()}: "
            if obj.is_value_field():
                if obj:
                    yamlfied_val = yaml.dump(
                        {"PLCHLDR_VAL": obj.field_props.set_value}
                    ).lstrip("PLCHLDR_VAL: ")
                line
                comment = self._generate_comment_header(obj.field_props)

    def _generate_comment_header(self, field: ConfigFieldProps) -> List[str]:
        header_lines: List[str] = []
        header_lines.append(f"## {field.name} - {field.type} \n")
        if field.description:
            header_lines.append(f"## Description: {field.description} \n")
        if field.enum:
            header_lines.append(f"## allowed values: {field.enum} \n")
        if field.has_default():
            header_lines.append(f"## defaults to {field.default} \n")
        if field.example:
            header_lines.append(f"## example: `{field.example}`\n")
        header_lines.append(
            f"# EnvVar name to override: '{field.get_env_name(self.environment_var_prefix)}'\n"
        )
        return header_lines


import os, sys

if __name__ == "__main__":
    SCRIPT_DIR = os.path.dirname(
        os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__)))
    )
    MODULE_ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
    sys.path.insert(0, os.path.normpath(MODULE_ROOT_DIR))
from onbot.config import ConfigDefaultModel

y = YamlConfigFileHandler(ConfigDefaultModel, "ccc.yml")
y.generate_example_config_file()
"""
c = ConfigHandler(
    config_model_class=ConfigDefaultModelDEMO,
    config_file="config.yml",
    environment_var_prefix="TEST",
)
"""
# c.get_fields_filler()
# c.generate_config_file(target_path="exampl.yml")
# c.load_config()
# config = c.get_config()
# print(type(config))
# print(yaml.dump(config.dict(), sort_keys=False))
