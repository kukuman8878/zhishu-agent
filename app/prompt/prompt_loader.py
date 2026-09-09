"""
Prompt 模板加载工具

按名称从项目根目录的 prompts 目录读取 .prompt 文件
业务节点只需要传入逻辑名称，不需要关心提示词文件的具体路径
"""

from pathlib import Path


# 按逻辑名从根目录 prompts 目录加载 .prompt 模板内容并返回字符串；参数 name 为 prompt 文件逻辑名（不含扩展名）
def load_prompt(name: str) -> str:
    """读取指定名称的 prompt 模板内容"""

    # app/prompt/prompt_loader.py 向上两级回到项目根目录，再进入 prompts 目录
    prompt_path = Path(__file__).parents[2] / "prompts" / f"{name}.prompt"
    return prompt_path.read_text(encoding="utf-8")
