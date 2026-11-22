"""Problem image rendering, asset caching and object storage."""

# Every PNG starts with these bytes. Defined here rather than in pipeline.py so the
# bot can check images without importing the render pipeline.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
