import typing
import asyncio
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
    Awaitable,
)
import inspect
from functools import singledispatch
from pydantic import BaseModel, fields, BaseSettings, schema
from pathlib import Path, PurePath
import yaml
from dataclasses import dataclass


def get_nested_dict_val_by_path(
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
            get_nested_dict_val_by_path(data[key_path[0]], key_path[1:], fallback_val)
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


def synchronize_async_helper(to_await_func: Awaitable):
    # https://stackoverflow.com/a/71489745/12438690
    async_response = []

    async def run_and_capture_result():
        r = await to_await_func
        async_response.append(r)

    loop: asyncio.BaseEventLoop = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    coroutine = run_and_capture_result()
    loop.run_until_complete(coroutine)
    print("async_response", async_response)
    return async_response[0]


# ToDo: Wrap that class up and migrate that into a proper selfcontained python module
# * Add generate markdown function
# * complete _generate_file() with all parameters
class YamlConfigFileHandler:
    @dataclass
    class FieldInfoContainer:
        field_schema: fields.ModelField = None
        container_model: BaseModel = None
        env_var_name: str = None

    def __init__(self, model: Type[BaseSettings], file_path: Union[str, Path] = None):
        self.config_file: Path = (
            file_path if isinstance(file_path, Path) else Path(file_path)
        )
        self.model = model

    def get_config(self):
        with open(self.config_file) as file:
            raw_yaml_object = file.read()
        obj: Dict = yaml.safe_load(raw_yaml_object)
        return self.model.parse_obj(obj)

    def generate_config_file(self, overwrite_existing: bool = False, exists_ok=True):
        null_placeholder = "NULL_PLACEHOLDER_328472384623746012386389621948"
        dummy_values = self._get_fields_filler(
            required_only=False,
            use_example_values_if_exists=False,
            fallback_fill_value=null_placeholder,
        )
        config = self.model.parse_obj(dummy_values)
        self._generate_file(
            config,
            overwrite_existing=overwrite_existing,
            generate_with_example_values=True,
            exists_ok=exists_ok,
            replace_pattern={null_placeholder: "null"},
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
        exists_ok: bool = False,
        generate_with_optional_fields: bool = True,
        comment_out_optional_fields: bool = True,
        generate_with_comment_desc_header: bool = True,
        generate_with_example_values: bool = False,
        replace_pattern: Dict = None,
    ):
        self.config_file.parent.mkdir(exist_ok=True, parents=True)
        if self.config_file.is_file() and not overwrite_existing:
            if exists_ok:
                return
            else:
                raise FileExistsError(
                    f"Can not generate config file at {self.config_file}. File allready exists."
                )
        if replace_pattern is None:
            replace_pattern = {}
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
            if line_no_indent.startswith("- "):
                # we are in list element
                pass

            elif ": " in line:
                key, val = line.split(": ")
                key = key.strip()
                field = self._get_field_info(key, current_path)
                if field:
                    comment = self.generate_field_header(
                        field,
                        indent_size=depth * 2,
                    )
                    if comment:
                        yaml_content_with_comment.append(comment)
                previous_key = key
            elif line.endswith(":"):
                key = line.split(":")[0].strip()
                field = self._get_field_info(key, current_path)
                if field:
                    comment = self.generate_field_header(
                        field,
                        indent_size=depth * 2,
                    )
                    if comment:
                        yaml_content_with_comment.append(comment)
                # sub chapter or line start
                previous_key = key
            previous_depth = depth
            yaml_content_with_comment.append(line)
        with open(self.config_file, "w") as file:
            lines = []
            for line in yaml_content_with_comment:
                for key, val in replace_pattern.items():
                    lines.append(f"{line.replace(key, val)}\n")
            file.writelines(lines)

    def _get_field_info(self, key: str, path: List[str]) -> FieldInfoContainer | None:
        info = YamlConfigFileHandler.FieldInfoContainer()
        info.container_model = self.model
        info.field_schema = self.model
        env_var_delimiter: str = (
            self.model.Config.env_nested_delimiter
            if self.model.Config.env_nested_delimiter
            else "__"
        )
        env_var_prefix: str = self.model.Config.env_prefix
        for parent_key in path + [key]:
            if isinstance(info.field_schema, fields.ModelField):
                if (
                    inspect.isclass(info.field_schema.type_)
                    and issubclass(info.field_schema.type_, BaseModel)
                    and parent_key in info.field_schema.type_.__fields__
                ):
                    info.container_model = info.field_schema.type_
                    info.field_schema = info.field_schema.type_.__fields__[parent_key]
                else:
                    return None
            else:
                info.container_model = info.field_schema
                info.field_schema = info.field_schema.__fields__[parent_key]
        info.env_var_name = env_var_delimiter.join(
            [env_var_prefix + k.upper() for k in path + [key]]
        )
        return info

    def generate_field_header(self, field: FieldInfoContainer, indent_size: int = 0):
        if not isinstance(field.field_schema, fields.ModelField):
            return None
        indent = f"{' '*indent_size}"
        field_info = field.field_schema.field_info
        parent_schema = schema.model_schema(field.container_model)
        field_schema = parent_schema["properties"][field.field_schema.name]
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
        header_lines.append(
            f"### {field_schema['title']} - '{field.field_schema.name}'###"
        )
        if "type" in field_schema:
            header_lines.append(f"# Type: {field_schema['type']}")
        if field_info.description:
            desc = field_info.description.replace("\n", f"\n{indent}#   ")
            header_lines.append(f"# Description: {desc}")

        header_lines.append(f"# Required: {field.field_schema.required}")
        if "enum" in field_schema:
            header_lines.append(f"# Allowed values: {field_schema['enum']}")
        if field.field_schema.default:
            header_lines.append(f"# Defaults to {field.field_schema.default}")
        header_lines.append(f"# EnvVar name to override: '{field.env_var_name}'")
        if "example" in field_info.extra:
            exmpl = f"\n" + yaml.dump(
                {
                    field.field_schema.name: self.jsonfy_example(
                        field_info.extra["example"]
                    )
                }
            )
            exmpl = exmpl.rstrip("\n")
            exmpl = exmpl.replace("\n", f"\n{indent}# >{indent}")
            header_lines.append(f"# Example: {exmpl}")

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
                        elif field.default_factory is not None:
                            print(key, field.default_factory)
                            result[key] = field.default_factory()
                        else:
                            result[key] = parse_model_class(field.type_)
                    elif isinstance(
                        field.type_, (str, int, float, complex, list, dict, set, tuple)
                    ):
                        result[key] = self.jsonfy_example(field.type_())
                    elif (
                        isinstance(field, fields.ModelField)
                        and field.default_factory is not None
                    ):
                        val = self.jsonfy_example(field.default_factory())
                        print(type(val), val)
                        result[key] = self.jsonfy_example(field.default_factory())
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
            return str(val)
