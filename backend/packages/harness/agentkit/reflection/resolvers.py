from importlib import import_module

MODULE_TO_PACKAGE_HINTS = {
    "langchain_anthropic": "langchain-anthropic",
    "langchain_openai": "langchain-openai",
    "langchain_deepseek": "langchain-deepseek",
}

def resolve_variable[T](variable_path: str, expected_type: type[T] | tuple[type[T], ...] | None = None) -> T:
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"Invalid variable path: {variable_path}, expected format: module_path:variable_name") from err
    
    try:
        module = import_module(module_path)
    except ImportError as err:
        module_root = module_path.split(".",1)[0]
        if isinstance(err, ModuleNotFoundError) and module_root == getattr(err, "name", None):
            raise ImportError(f"Could not import module {module_path}. Make sure the module is installed.") from err
        raise ImportError(f"Failed to import module: {module_path}") from err
    
    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise AttributeError(f"Module {module_path} has no attribute {variable_name}") from err
    
    if expected_type is not None and not isinstance(variable, expected_type):
        raise ValueError(f"{variable_path} is not of type {expected_type}, got {type(variable).__name__}")