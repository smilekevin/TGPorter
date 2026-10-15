"""Import Kurigram through its Pyrogram-compatible namespace without an implicit event-loop dependency."""

import asyncio


# Kurigram captures a loop while wrapping its synchronous API during import.
# Python 3.14 no longer creates that loop implicitly. Keep this temporary loop
# separate from the downloader loop, which Application owns and initializes.
try:
    asyncio.get_event_loop()
except RuntimeError:
    _import_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_import_loop)
    try:
        import pyrogram
    finally:
        asyncio.set_event_loop(None)
        _import_loop.close()
else:
    import pyrogram
