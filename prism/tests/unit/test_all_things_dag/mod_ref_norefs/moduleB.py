from prism.task import PrismTask
import prism.target as PrismTarget

class Moduleb(PrismTask):

    def run(self, mods, hooks):
        return "This is module B."


# EOF