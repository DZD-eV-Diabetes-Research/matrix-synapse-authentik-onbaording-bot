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
from pydantic import BaseModel, fields, BaseSettings, schema
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


class YamlConfigFileHandler:
    def __init__(self, model: Type[BaseSettings], file_path: Union[str, Path] = None):
        self.config_file: Path = (
            file_path if isinstance(file_path, Path) else Path(file_path)
        )
        self.model = model

    def get_config(self):
        raw_yaml: str = None
        with open(self.config_file) as file:
            raw_yaml_object = file.read()
        obj: Dict = yaml.safe_load(raw_yaml_object)
        return self.model.parse_obj(obj)

    def generate_config_file(self, overwrite_existing: bool = False):
        dummy_values = self._get_fields_filler(
            required_only=False, use_example_values_if_exists=False
        )
        print(dummy_values["matrix_user_ignore_list"])

        config = self.model.parse_obj(dummy_values)
        self._generate_file(
            config,
            overwrite_existing=overwrite_existing,
            generate_with_example_values=True,
        )

    def generate_example_config_file(self):
        dummy_values = self._get_fields_filler(
            required_only=True, use_example_values_if_exists=True
        )
        # print(dummy_values)
        config = self.model.parse_obj(dummy_values)
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
        yaml_content: str = yaml.dump(config.dict(), sort_keys=False)
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
                field_info, parent_model = self._get_field_info(key, current_path)
                if field_info:
                    # field = self._get_field_info(key, current_path)
                    comment = self.generate_field_header(
                        field_info,
                        parent_model,
                        indent_size=depth * 2,
                    )
                    if comment:
                        yaml_content_with_comment.append(comment)
                previous_key = key
            elif line.endswith(":"):
                key = line.split(":")[0].strip()
                field_info, parent_model = self._get_field_info(key, current_path)
                if field_info:
                    comment = self.generate_field_header(
                        field_info,
                        parent_model,
                        indent_size=depth * 2,
                    )
                    if comment:
                        yaml_content_with_comment.append(comment)
                # sub chapter or line start
                previous_key = key
            previous_depth = depth
            yaml_content_with_comment.append(line)
        with open(self.config_file, "w") as file:
            file.writelines(f"{s}\n" for s in yaml_content_with_comment)

    def _get_field_info(
        self, key: str, path: List[str]
    ) -> Tuple[fields.ModelField, BaseModel] | Tuple[None, None]:
        parent_model = self.model
        current_obj = self.model
        for parent_key in path + [key]:
            if isinstance(current_obj, fields.ModelField):
                if (
                    inspect.isclass(current_obj.type_)
                    and issubclass(current_obj.type_, BaseModel)
                    and parent_key in current_obj.type_.__fields__
                ):
                    parent_model = current_obj.type_
                    current_obj = current_obj.type_.__fields__[parent_key]
                else:
                    return None, None
            else:
                parent_model = current_obj
                current_obj = current_obj.__fields__[parent_key]
        return current_obj, parent_model

    def generate_field_header(
        self, field: fields.ModelField, parent_model: BaseModel, indent_size: int = 0
    ):
        if not isinstance(field, fields.ModelField):
            return None
        indent = f"{' '*indent_size}"
        field_info = field.field_info
        parent_schema = schema.model_schema(parent_model)
        field_schema = parent_schema["properties"][field.name]
        """ value examples
        field: name='server_name' type=Optional[ConstrainedStrValue] required=False default=None
        field_info: description="Synapse's public facing domain https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name" max_length=100 extra={'example': 'company.org'}
        field_schema: {'title': 'Server Name', 'description': "Synapse's public facing domain https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name", 'maxLength': 100, 'example': 'company.org', 'type': 'string'}
        """
        """
        if field.name == "synapse_server":
            print("field:", field)
            print("field_info:", field_info)
            print("field_schema:", field_schema)
        """
        # YOU ARE HERE. seems all good :)
        header_lines: List[str] = []
        header_lines.append(f"### {field_schema['title']} - '{field.name}'###")
        if "type" in field_schema:
            header_lines.append(f"# Type: {field_schema['type']}")
        if field_info.description:
            desc = field_info.description.replace("\n", f"\n{indent}#")
            header_lines.append(f"# Description: {desc}")
        header_lines.append(f"# Required: {field.required}")
        if "enum" in field_schema:
            header_lines.append(f"# Allowed values: {field_schema['enum']}")
        if field.default:
            header_lines.append(f"# Defaults to {field.default}")
        if "example" in field_info.extra:
            exmpl = f"\n" + yaml.dump(
                {field.name: self.jsonfy_example(field_info.extra["example"])}
            )
            exmpl = exmpl.rstrip("\n")
            exmpl = exmpl.replace("\n", f"\n{indent}# >{indent}")
            header_lines.append(f"# Example: {exmpl}")
        """
        header_lines.append(
            f"# EnvVar name to override: '{field.get_env_name(self.environment_var_prefix)}'"
        )
        """

        return "\n" + "\n".join([f"{indent}{line}" for line in header_lines])

    def _get_fields_filler(
        self,
        required_only: bool = True,
        use_example_values_if_exists: bool = False,
        fallback_fill_value: Any = "",
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
                    if (
                        use_example_values_if_exists
                        and "example" in field.field_info.extra
                    ):
                        result[key] = self.jsonfy_example(
                            field.field_info.extra["example"]
                        )
                    elif inspect.isclass(field.type_) and issubclass(
                        field.type_, BaseModel
                    ):
                        if field.default is not None:
                            result[key] = field.default
                        else:
                            result[key] = parse_model_class(field.type_)
                    elif isinstance(
                        field.type_, (str, int, float, complex, list, dict, set, tuple)
                    ):
                        result[key] = self.jsonfy_example(field.type_())
                    else:
                        result[key] = (
                            fallback_fill_value
                            if field.required
                            else self.jsonfy_example(field.default)
                        )
            return result

        return parse_model_class(self.model)

    def jsonfy_example(self, val: Any) -> List | Dict:
        if isinstance(val, dict):
            result: Dict = {}
            for k, v in val.items():
                result[k] = self.jsonfy_example(v)
            return result
        elif isinstance(val, (list, set, tuple)):
            return [self.jsonfy_example(i) for i in val]
        elif isinstance(val, BaseModel):
            return val.json()
        else:
            # if isinstance(val, tuple):
            return val


import os, sys

if __name__ == "__main__":
    SCRIPT_DIR = os.path.dirname(
        os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__)))
    )
    MODULE_ROOT_DIR = os.path.join(SCRIPT_DIR, "..")
    sys.path.insert(0, os.path.normpath(MODULE_ROOT_DIR))
from onbot.config import ConfigDefaultModel

y = YamlConfigFileHandler(ConfigDefaultModel, "ccc.yml")
y.generate_config_file(overwrite_existing=True)
print(y.get_config())
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
