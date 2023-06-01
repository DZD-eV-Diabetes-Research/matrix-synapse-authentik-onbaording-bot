from typing import List, Any, Dict

from functools import singledispatch


def walk_attr_path(data: Dict, keys: List[str], default: Any = Any) -> Any:
    # i know `default` solution as optional is hacky. Overloading in python is hard :)
    # thanks to https://stackoverflow.com/a/47969823/12438690
    try:
        return walk_attr_path(data[keys[0]], keys[1:], default) if keys else data
    except KeyError:
        if default != Any:
            return default
        else:
            raise
