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
)

from functools import singledispatch
from pydantic import BaseModel
from pathlib import Path, PurePath
import yaml


def get_nested_dict_attr_by_path(
    data: Dict, keys: List[str], fallback_val: Any = Any
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
            get_nested_dict_attr_by_path(data[keys[0]], keys[1:], fallback_val)
            if keys
            else data
        )
    except KeyError:
        if fallback_val != Any:
            return fallback_val
        else:
            raise


def create_nested_dict(path: str, value: Any, path_seperator: str = ".") -> Dict:
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
        print(parent_config_chapter.get_path(), pydantic_schema_item_properties)
        self.parent_config_chapter = parent_config_chapter
        self.type_hint = type_hint
        self.set_value = set_value

    @property
    def value(self):
        if self.set_value == ConfigValueNotSet and not self.has_default():
            # TODO: You are here. problem is that config var with ..."= None" are not accepded as default = None
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
            if self.parent_attr:
                path.append(self.parent_attr)
            if self.parent_key:
                path.append(self.parent_key)
            if self.parent_index:
                path.append(self.parent_index)
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
            for child_obj in ConfigHandler()._walk_config(
                self.config_object, max_depth=1
            ):
                if not child_obj.is_optional():
                    return False


class ConfigHandler:
    def __init__(
        self,
        config_model_class: Type[BaseModel],
        config_file: Union[str, Path],
        environment_var_prefix: str = None,
    ):
        self.config_model_class = config_model_class
        self.config_file: Path = (
            config_file if isinstance(config_file, Path) else Path(config_file)
        )
        self.environment_var_prefix: str = (
            environment_var_prefix if environment_var_prefix else ""
        )
        self._resulting_config: BaseModel = None

    def load_config(self, reload: bool = False):
        """_summary_

        Args:
            reload (bool, optional): If set to True; reload data from config file and env variables. Defaults to False.
        """
        if self._resulting_config is None or reload:
            # load default config
            # self._resulting_config = self.config_model_class.construct()

            # load file to override default config
            config_file_content = None

            with open(self.config_file, "r") as stream:
                config_file_content = yaml.safe_load(stream)

            # merge config from file onto default config
            self._resulting_config = self.config_model_class.parse_obj(
                config_file_content
            )
            # merge config from environment variables on top
            self._resulting_config = self.config_model_class.parse_obj(
                self._resulting_config.dict()
                | self.load_config_from_environment_var(self._resulting_config)
            )

        # TODO_: YOU ARE HERE!!!! You try to deconstruct the config object to reconstruct a yaml file field by field and add comments from the field object
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

                # exit()
            # this make no sense, we always get the full config instead of single chapers

    def load_config_from_environment_var(self, config: BaseModel) -> Dict:
        values: Dict = {}
        for obj in self._walk_config(config):
            if obj.is_value_field():
                environment_var_name = obj.field_props.get_env_name(
                    self.environment_var_prefix
                )
                if environment_var_name in os.environ:
                    value = os.getenv(environment_var_name)
                    values = values | create_nested_dict(
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
            config_obj = self.config_model_class()
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
        if field.default:
            header_lines.append(f"## defaults to {field.default} \n")
        if field.example:
            header_lines.append(f"## example: `{field.example}`\n")
        header_lines.append(f"# EnvVar name to override: '{field.get_env_name()}'\n")
        return header_lines


import os, sys

if __name__ == "__main__":
    SCRIPT_DIR = os.path.dirname(
        os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__)))
    )
    MODULE_ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
    sys.path.insert(0, os.path.normpath(MODULE_ROOT_DIR))
from onbot.config import ConfigDefaultModel

c = ConfigHandler(
    config_model_class=ConfigDefaultModel,
    config_file="config.yml",
    environment_var_prefix="TEST",
)
c.load_config()
config = c.get_config()
# print(type(config))
# print(yaml.dump(config.dict(), sort_keys=False))
