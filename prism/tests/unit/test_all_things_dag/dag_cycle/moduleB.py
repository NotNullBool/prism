from prism.task import PrismTask
import prism.target as PrismTarget

class Moduleb(PrismTask):

    def run(self, mods, hooks):
        return mods.ref('moduleA.py') + mods.ref('moduleE.py') + " This is module B."


# EOF