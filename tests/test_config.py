"""The settings file."""
import tempfile
import unittest
from pathlib import Path

from pulley import config
from pulley.config import ConfigError, parse

ROOT = Path(tempfile.gettempdir()).resolve().as_posix()


def one(more: str = "", name: str = "shop") -> str:
    return f'[[project]]\nname = "{name}"\npath = "{ROOT}/apps/{name}"\n{more}\n'


class Settings(unittest.TestCase):
    def refused(self, text: str, saying: str) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse(text)
        self.assertIn(saying, str(caught.exception))

    def test_empty(self):
        cfg = parse("")
        self.assertEqual((cfg.projects, cfg.check_every), ([], 6 * 3600))

    def test_defaults(self):
        p = parse(one()).projects[0]
        self.assertEqual((p.name, p.service, p.follow, p.remote, p.tags), ("shop", "shop", "branch", "origin", "*"))
        self.assertEqual((p.health_wait, p.settle, p.build_wait), (120, 20, 1800))
        self.assertEqual(p.path, Path(f"{ROOT}/apps/shop"))
        self.assertFalse(p.prereleases or p.skip_path_check)

    def test_everything_set(self):
        p = parse('check_every = "30m"\n' + one(
            'service = "web"\ncompose = "deploy/compose.yaml"\nfollow = "tags"\ntags = "v*"\nprereleases = true\n'
            'remote = "upstream"\nproject_name = "store"\nhealth_url = "http://10.0.0.5:8080/ok"\n'
            'health_wait = "3m"\nsettle = 45\nbuild_wait = "1h"\nskip_path_check = true')).projects[0]
        self.assertEqual((p.service, p.compose, p.follow, p.tags, p.remote, p.project_name),
                         ("web", "deploy/compose.yaml", "tags", "v*", "upstream", "store"))
        self.assertEqual((p.health_wait, p.settle, p.build_wait), (180, 45, 3600))
        self.assertTrue(p.prereleases and p.skip_path_check)

    def test_lengths_of_time(self):
        self.assertEqual(parse('check_every = "never"').check_every, 0)
        self.assertEqual(parse("check_every = 0").check_every, 0)
        self.assertEqual(parse("check_every = 900").check_every, 900)
        self.assertEqual(parse('check_every = "1d"').check_every, 86400)
        self.refused('check_every = "soon"', "length of time")
        self.refused("check_every = 1.5", "length of time")
        self.refused("check_every = true", "length of time")
        self.refused('check_every = "10s"', "cannot be shorter than 300 seconds")

    def test_refusals(self):
        self.refused("project = 3", "double brackets")
        self.refused("chek_every = 5", "does not know: chek_every")
        self.refused('[[project]]\npath = "/x"', "needs a name")
        self.refused(one(name="My Shop"), "needs a name")
        for bad in ("my..app", "app-", "-app", "a_-b", "x" * 64):
            self.refused(one(name=bad), "needs a name")
        for good in ("a", "my-app", "my_app.v2", "x" * 63):
            self.assertEqual(parse(one(name=good)).projects[0].name, good)
        self.refused('[[project]]\nname = "shop"', "needs path")
        self.refused('[[project]]\nname = "shop"\npath = "apps/shop"', "needs path")
        self.refused(one('helth_url = "http://x"'), 'Project "shop" has a setting Pulley does not know: helth_url')
        self.refused(one('follow = "releases"'), 'follow should be "branch" or "tags"')
        self.refused(one("service = 5"), "service should be text")
        self.refused(one('prereleases = "yes"'), "prereleases should be true or false")
        self.refused(one('health_url = "ftp://x"'), "health_url should start with http")
        self.refused(one('health_wait = "never"'), "health_wait cannot be never")
        self.refused(one('health_wait = "10s"\nsettle = "10s"'), "settle has to be shorter")
        self.refused(one() + one(), 'Two projects are called "shop"')
        self.refused("[[project]\n", "not valid TOML")

    def test_compose_file_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            p = parse(f'[[project]]\nname = "shop"\npath = "{folder.as_posix()}"').projects[0]
            self.assertIsNone(p.compose_file())
            (folder / "docker-compose.yml").write_text("services: {}\n", "utf-8")
            self.assertEqual(p.compose_file(), folder / "docker-compose.yml")
            (folder / "compose.yaml").write_text("services: {}\n", "utf-8")
            self.assertEqual(p.compose_file(), folder / "compose.yaml", "the newer name wins")
            p.compose = "deploy/stack.yaml"
            self.assertIsNone(p.compose_file())
            (folder / "deploy").mkdir()
            (folder / "deploy" / "stack.yaml").write_text("services: {}\n", "utf-8")
            self.assertEqual(p.compose_file(), folder / "deploy" / "stack.yaml")

    def test_missing_file_gets_an_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "data" / "pulley.toml"
            cfg = config.load(file)
            self.assertEqual(cfg.projects, [])
            self.assertIn("[[project]]", file.read_text("utf-8"))
            self.assertEqual(config.load(file).check_every, 6 * 3600, "the example itself is valid")

    def test_example_block_is_valid_once_uncommented(self):
        lines = []
        for line in config.EXAMPLE.splitlines():
            lines.append(line[2:] if line.startswith("# ") and ("=" in line or line.startswith("# [[")) else line)
        text = "\n".join(lines).replace('"/srv/apps/myapp"', f'"{ROOT}/apps/myapp"')
        p = parse(text).projects[0]
        self.assertEqual((p.name, p.branch, p.health_wait), ("myapp", "main", 120))


if __name__ == "__main__":
    unittest.main()
