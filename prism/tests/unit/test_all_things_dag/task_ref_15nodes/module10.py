from prism.task import PrismTask
import prism.target as PrismTarget

class Task10(prism.task.PrismTask):

    def run(self, tasks, hooks):
        return tasks.ref('task01.py') + "This is task 10. "


# EOF