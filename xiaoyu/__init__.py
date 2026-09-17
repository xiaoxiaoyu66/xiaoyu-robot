"""XiaoYu Robot —— 桌面陪伴机器人。

代码结构约定：
    xiaoyu/           主包，所有业务代码
        logger.py     日志（全项目唯一出口）
        config.py     配置（路径 / 参数 / 密钥）
        state.py      状态机（idle / listening / thinking / speaking）
        audio/        录音、播放、设备自检
        wake/         唤醒词
        asr/          语音转文字
        tts/          文字转语音
        llm/          大模型对话
        memory/       长短期记忆
        vision/       视觉（读字 / 认人，后期）
    scripts/          一次性脚本（自检、下模型）
    config/           可编辑的文本配置（唤醒词、性格）
    models/           模型文件（不进 git）
    data/             SQLite 等运行期数据（不进 git）
    logs/             日志文件（不进 git）
    docs/             设计文档
    tests/            测试
"""

__version__ = "0.1.0"
__all__ = ["__version__"]