from .base import Agent, PROJECT_DIR
from config import TIMEOUT_REVIEWER

class ReviewerAgent(Agent):
    def __init__(self):
        super().__init__("Reviewer")

    def review(self, issue_number: int, continue_session: bool = False) -> list[str]:
        prompt = (
            f"审查 Issue #{issue_number} 的本地修复，用 git diff 查看具体变更，用 git log -1 看提交信息，运行相关测试。"
            f"关注：1.是否完整真正修复了 issue  2.是否引入副作用或破坏现有功能  3.是否误改了无关文件  4.代码风格是否与项目一致  5.编译/测试是否通过。"
            f"每条意见前加序号。如果没问题输出 LGTM"
        )
        output = self.run(prompt, timeout=TIMEOUT_REVIEWER, continue_session=continue_session)
        if "LGTM" in output.upper():
            return []
        return [s.strip() for s in output.split("\n\n") if s.strip()]
