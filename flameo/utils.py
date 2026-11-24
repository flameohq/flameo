import asyncio
from typing import Optional

_base64 = None


async def base64_to_file(base64String):
    global _base64
    if _base64 is None:
        import base64
        _base64 = base64
    byte_content = await asyncio.to_thread(_base64.b64decode, base64String)
    return byte_content


def template_to_string(template_name: str, data: Optional[dict] = None, filters=None) -> str:
    """Render a template from the app's template folder into a string.

    The ``template_name`` is relative to the folder registered earlier via
    ``app.use_template()``. ``data`` is the variable dictionary bound to the
    template and ``filters`` accepts the same shapes as ``use_template()``:
    a list of callables (named by ``__name__``) or a dict of
    ``{"filter_name": callable}``.

    Raises ``RuntimeError`` if no template folder has been registered yet.
    """
    from .templates import (
        get_last_template_directory,
        build_template_string_environment,
    )

    directory = get_last_template_directory()
    if directory is None:
        raise RuntimeError(
            "Initialize 'app.use_template()' before calling "
            "template_to_string() -- no template folder registered yet."
        )

    env = build_template_string_environment(directory, filters)
    enable_async = getattr(env, "is_async", False)

    template = env.get_template(template_name)
    variables = data if data is not None else {}
    if not enable_async:
        return template.render(variables)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(template.render_async(variables))
    # Called from inside a running event loop -- run the async render on a
    # throwaway loop in a worker thread so this synchronous call can still
    # block for its result without deadlocking the caller's loop.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, template.render_async(variables)).result()
