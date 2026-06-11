from .base import Agent


class SubmitterAgent(Agent):
    def __init__(self):
        super().__init__("Submitter")

    def submit(self, issue_number: int, continue_session: bool = False) -> str:
        prompt = (
            f"为 Issue #{issue_number} 的修复创建新分支，commit，push，创建 PR（base: main），"
            f"PR 描述中关联 #{issue_number}。提交描述和评论请使用英文。issue中也说明完成了哪些内容。最后输出 PR_URL=<链接>"
        )
        output = self.run(prompt, timeout=600, continue_session=continue_session)
        pr_url = self.extract(output, r"PR_URL=(\S+)")
        if pr_url is None:
            pr_url = self.extract(output, r"https://github\.com/[^\s]+/pull/\d+")
        return pr_url or ""
