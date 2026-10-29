"""Hatchling hook. Tests load ``stage_data.py`` by path and do not import this module."""

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Both archives take skills/ and setup/ from the staged, git-tracked set (stage_data), never
        # from a raw directory walk. The sdist keeps them as source; only the wheel relocates them
        # under agentcompass/data so an installed copy still has real files.
        if self.target_name not in ("wheel", "sdist"):
            return
        import sys
        from pathlib import Path

        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.append(str(here))
        import stage_data

        staged = stage_data.stage(Path(self.root))
        if self.target_name == "sdist":
            build_data["force_include"][str(staged / "skills")] = "skills"
            build_data["force_include"][str(staged / "setup")] = "setup"
            return
        build_data["force_include"][str(staged / "skills")] = "agentcompass/data/skills"
        build_data["force_include"][str(staged / "setup")] = "agentcompass/data/setup"
