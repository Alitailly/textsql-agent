"""主 Agent 运行时可挂的工具名。

说话不是工具。查数工具默认挂载；联网搜索默认不挂；bash / 写文件关掉。
"""

QUERY_TOOL = "查数工具"
WEB_SEARCH = "联网搜索"
BASH = "bash"
WRITE_FILE = "写文件"

DEFAULT_MOUNTED_TOOLS: tuple[str, ...] = (QUERY_TOOL,)
