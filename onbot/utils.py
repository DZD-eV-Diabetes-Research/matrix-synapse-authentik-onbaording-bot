from typing import List, Any, Dict, Union, Literal, Tuple, Type, Generator

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

    # i know `default` solution as optional is hacky. Overloading in python is hard :)
    # thanks to https://stackoverflow.com/a/47969823/12438690
    try:
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


class ConfigFieldProps:
    def __init__(
        self,
        name: str,
        pydantic_schema_item_properties: Dict,
    ):
        self.name = name
        if "title" in pydantic_schema_item_properties:
            self.title = pydantic_schema_item_properties["title"]
        if "description" in pydantic_schema_item_properties:
            self.description = pydantic_schema_item_properties["description"]
        if "example" in pydantic_schema_item_properties:
            self.example = pydantic_schema_item_properties["example"]
        if "alias" in pydantic_schema_item_properties:
            self.alias = pydantic_schema_item_properties["alias"]
        if "type" in pydantic_schema_item_properties:
            self.type = pydantic_schema_item_properties["type"]


class ConfigChapterMetaContainer:
    def __init__(
        self,
        config_object: Any,
        parent: "ConfigChapterMetaContainer" = None,
        parent_attr: str = None,
        parent_key: str = None,
        parent_index: int = None,
        field_props: ConfigFieldProps = None,
    ):
        self.config_chapter = config_object
        self.parent_object = parent
        self.parent_attr = parent_attr
        self.parent_key = parent_key
        self.parent_index = parent_index
        self.field_props = field_props

    def get_path(self, base_only: bool = False) -> str:
        path = ""
        if self.parent_object:
            path = self.parent_object.get_path()
            if self.parent_attr:
                path += f"[{self.parent_attr}]"
            if self.parent_key:
                path += f"[{self.parent_key}]"
            if self.parent_index:
                path += f"[{self.parent_index}]"
        return path


class default(BaseModel):
    hello: int = 1
    l = list[1, 2, 3]


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

        # load default config
        self._resulting_config = self.config_model_class()

        # load file to override default config
        config_file_content = None

        with open(self.config_file, "r") as stream:
            config_file_content = yaml.safe_load(stream)

        # merge config from file onto default config
        self._resulting_config = self.config_model_class.parse_obj(
            self.config_model_class().dict() | config_file_content
        )

        # TODO_: YOU ARE HERE!!!! You try to deconstruct the config object to reconstruct a yaml file field by field and add comments from the field object
        for obj in self._walk_config(self._resulting_config):
            print("###")
            print(type(obj))
            # this make no sense, we always get the full config instead of single chapers
            print(obj.config_chapter)

    def load_config_from_environment_var(self):
        pass

    def _walk_config(
        self,
        config_obj: Union[BaseModel, List, Dict],
        parent: ConfigChapterMetaContainer = None,
        parent_attr: str = None,
        parent_key: str = None,
        parent_index: int = None,
        field_props: ConfigFieldProps = None,
    ) -> Generator[ConfigChapterMetaContainer, None, None]:
        this = ConfigChapterMetaContainer(
            config_obj,
            parent,
            parent_attr=parent_attr,
            parent_key=parent_key,
            parent_index=parent_index,
            field_props=field_props,
        )
        yield this
        if isinstance(config_obj, list):
            yield this
            for index, child_obj in enumerate(config_obj):
                self._walk_config(child_obj, parent=this, parent_index=index)
        elif isinstance(config_obj, dict):
            yield this
            for key, child_obj in config_obj.items():
                self._walk_config(child_obj, parent=this, parent_key=key)
        elif isinstance(config_obj, BaseModel):
            pydantic_field_props: Dict = this.config_chapter.schema()["properties"]
            yield this

            for attr, child_obj in config_obj.dict().items():
                self._walk_config(
                    child_obj,
                    parent=this,
                    parent_attr=attr,
                    field_props=ConfigFieldProps(
                        attr, pydantic_schema_item_properties=pydantic_field_props[attr]
                    ),
                )

    def get_config(self) -> BaseModel:
        return self._resulting_config

    def generate_default_config(
        self,
        target_path: Union[str, Path] = None,
        overwrite_existing: bool = False,
        generate_with_optional_fields: bool = True,
        generate_with_example_values: bool = False,
        generate_with_comment_desc_header: bool = True,
        format: Literal["yaml"] = "yaml",
    ):
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

    def _generate_comment_header(
        self, pydantic_schema_item_properties: Tuple, item_base_path: str = None
    ) -> List[str]:
        if item_base_path is None:
            item_base_path = ""
        config_var_name = pydantic_schema_item_properties[0]
        config_var_info = pydantic_schema_item_properties[1]
        header_lines: List[str] = []
        header_lines.append(f"## {config_var_name} - {config_var_info['type']} \n")
        if "description" in config_var_info:
            header_lines.append(f"## Description: {config_var_info['description']} \n")
        if "enum" in config_var_info:
            header_lines.append(f"## allowed values: {config_var_info['enum']} \n")
        if "default" in config_var_info:
            header_lines.append(f"## defaults to {config_var_info['default']} \n")
        if "example" in config_var_info:
            header_lines.append(
                f"## example: `{self.environment_var_prefix}_{config_var_name}={config_var_info['example']}`\n"
            )
        env_var_name: str = f"{self.environment_var_prefix + '_' if self.environment_var_prefix else ''}{item_base_path + '_' if item_base_path else ''}{config_var_name}".upper()
        header_lines.append(f"# Env var name to override: '{env_var_name}'\n")
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
