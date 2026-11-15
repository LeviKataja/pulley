"""Updating, and what happens when the new version is no good. Real git, stand-in Docker."""
import json
import time
import unittest

from pulley import docker, engine
from pulley.engine import Busy, Manager
from world import WorldCase, git

PREVIOUS, OUTGOING = "pulley.local/shop:previous", "pulley.local/shop:outgoing"


class Looking(WorldCase):
    def test_up_to_date(self):
        p = self.look()
        self.assertEqual(p["state"], "ok")
        self.assertEqual(p["behind"], 0)
        self.assertEqual(p["current"]["subject"], "First version")
        self.assertEqual(p["follows"], "branch main")
        self.assertEqual(p["container"]["status"], "running")
        self.assertEqual(p["problems"], [])

    def test_sees_what_is_waiting(self):
        self.push(app="version 2", message="Second version")
        newest = self.push(app="version 3", message="Third version")
        p = self.look()
        self.assertEqual(p["state"], "waiting")
        self.assertEqual(p["behind"], 2)
        self.assertEqual([c["subject"] for c in p["changes"]], ["Third version", "Second version"])
        self.assertEqual(p["target"]["sha"], newest)
        self.assertNotEqual(self.head(), newest, "looking must not move anything")

    def test_edited_copy_is_left_alone(self):
        (self.work / "app.txt").write_text("version 1, tuned by hand\n", "utf-8")
        self.push(app="version 2")
        old, (cid, _) = self.head(), self.container()
        p = self.look()
        self.assertEqual(p["state"], "blocked")
        self.assertIn("edited by hand (app.txt)", p["problems"][0])
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn("Nothing was changed", entry["note"])
        self.assertEqual(self.head(), old)
        self.assertEqual(self.container()[0], cid)
        self.assertIn("tuned by hand", (self.work / "app.txt").read_text("utf-8"))

    def test_files_git_does_not_track_are_fine(self):
        (self.work / "data").mkdir()
        (self.work / "data" / "notes.db").write_text("x", "utf-8")
        self.push(app="version 2")
        self.assertEqual(self.look()["state"], "waiting")

    def test_copy_with_commits_of_its_own(self):
        (self.work / "local.txt").write_text("mine\n", "utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "A local commit")
        self.push(app="version 2")
        p = self.look()
        self.assertTrue(any("commits of its own" in text for text in p["problems"]), p["problems"])
        self.assertEqual(p["behind"], 0)

    def test_no_container(self):
        self.change_world(containers={})
        p = self.look()
        self.assertEqual(p["state"], "trouble")
        self.assertIn("cannot see a container", p["problems"][0])

    def test_stopped_container(self):
        cid, c = self.container()
        self.change_world(containers={cid: {**c, "status": "exited"}})
        self.push(app="version 2")
        p = self.look()
        self.assertEqual(p["state"], "blocked")
        self.assertIn("not running", p["problems"][0])

    def test_service_not_built_from_source(self):
        self.push(compose={"services": {"app": {"image": "ghcr.io/someone/shop:latest"}}})
        self.by_hand(pull=True)
        self.assertIn("not built from source", self.look()["problems"][0])

    def test_compose_file_wants_a_value_pulley_lacks(self):
        wants = {"services": {"app": {"build": ".", "command": "serve --key ${API_KEY} --zone ${TZ} ${API_KEY}"}}}
        self.change_world(env={"API_KEY": "secret", "TZ": "UTC"})  # the person's shell has them...
        self.push(compose=wants)
        self.by_hand(pull=True)
        self.change_world(env={})  # ...Pulley does not
        self.push(app="version 2")
        p = self.look()
        self.assertEqual(p["state"], "blocked")
        self.assertIn("uses API_KEY, TZ, which Pulley has no value for", p["problems"][0])
        cid = self.container()[0]
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "failed")
        self.assertEqual(self.container()[0], cid)

    def test_values_in_an_env_file_are_found(self):
        (self.work / ".env").write_text("API_KEY=secret\n", "utf-8")
        self.push(compose={"services": {"app": {"build": ".", "command": "serve --key ${API_KEY}"}}})
        self.by_hand(pull=True)
        self.push(app="version 2")
        self.assertEqual(self.look()["state"], "waiting")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        self.assertEqual(self.container()[1]["command"], "serve --key secret")

    def test_service_missing_from_compose_file(self):
        self.push(compose={"services": {"web": {"build": "."}}})
        self.by_hand("web", pull=True)
        self.assertIn("no such service: app", self.look()["problems"][0])

    def test_no_compose_file(self):
        self.settings('compose = "elsewhere.yaml"')
        self.assertIn("No compose file was found", self.look()["problems"][0])

    def test_compose_file_named_in_the_settings(self):
        self.settings('compose = "compose.yaml"')
        self.push(app="version 2")
        self.assertEqual(self.look()["state"], "waiting")

    def test_docker_not_answering(self):
        self.push(app="version 2")
        self.change_world(down=True)
        p = self.look()
        self.assertEqual(p["state"], "blocked")
        self.assertIn("Docker cannot be reached", p["problems"][0])
        self.assertIn("Cannot connect", self.mgr.state()["docker_error"])
        old = self.head()
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "failed")
        self.assertEqual(self.head(), old)

    def test_repository_out_of_reach(self):
        git(self.work, "remote", "set-url", "origin", str(self.origin.parent / "gone"))
        p = self.look()
        self.assertEqual(p["state"], "ok")
        self.assertIn("could not be reached", p["notes"][0])
        self.assertIn("does not appear to be a git repository", p["notes"][0], "git's own reason is passed on")

    def test_check_now_shows_at_once(self):
        self.push(app="version 2")
        before = self.mgr.state()["checked"]
        self.mgr.check_soon()
        self.assertTrue(self.mgr.state()["checking"], "the page must see it from the first moment")
        self.mgr.check_soon()  # a second press changes nothing
        deadline = time.monotonic() + 30
        while self.mgr.checking and time.monotonic() < deadline:
            time.sleep(0.05)
        state = self.mgr.state()
        self.assertGreater(state["checked"], before)
        self.assertEqual(state["projects"][0]["state"], "waiting")

    def test_settings_are_read_again(self):
        self.settings('follow = "tags"')
        self.assertEqual(self.look()["follows"], "version tags")
        (self.home / "pulley.toml").write_text("this is not toml [", "utf-8")
        self.mgr.check()
        state = self.mgr.state()
        self.assertIn("not valid TOML", state["config_error"])
        self.assertEqual([p["name"] for p in state["projects"]], ["shop"], "the earlier settings stay in use")


class Where(WorldCase):
    """Pulley in a container sees folders at paths that may differ from the machine's."""

    def in_container(self, mounts, me="pulley-itself"):
        self.mgr.own = lambda: (mounts, me)

    def test_same_path_on_both_sides(self):
        folder = self.work.parent.as_posix()
        self.in_container([("bind", folder, folder)])
        self.assertEqual(self.look()["problems"], [])

    def test_different_path_is_refused(self):
        self.push(app="version 2")
        self.in_container([("bind", "/mnt/tank/apps", self.work.parent.as_posix())])
        p = self.look()
        self.assertEqual(p["state"], "blocked")
        self.assertIn("on the machine it is /mnt/tank/apps/shop", p["problems"][0])
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "failed")

    def test_folder_only_inside_the_container(self):
        self.in_container([("bind", "/var/run/docker.sock", "/var/run/docker.sock")])
        self.assertIn("exists only inside Pulley's container", self.look()["problems"][0])

    def test_folder_in_a_docker_volume(self):
        self.in_container([("volume", "/var/lib/docker/volumes/apps/_data", self.work.parent.as_posix())])
        self.assertIn("exists only inside Pulley's container", self.look()["problems"][0])

    def test_check_can_be_switched_off(self):
        self.settings("skip_path_check = true")
        self.in_container([("bind", "/mnt/tank/apps", self.work.parent.as_posix())])
        self.assertEqual(self.look()["problems"], [])

    def test_own_container_is_refused(self):
        folder = self.work.parent.as_posix()
        self.in_container([("bind", folder, folder)], me=self.container()[0])
        self.assertIn("Pulley's own container", self.look()["problems"][0])

    def test_own_container_not_found_is_no_reason_to_refuse(self):
        def lost():
            raise docker.DockerError("Pulley could not find its own container.")
        self.mgr.own = lost
        self.assertEqual(self.look()["problems"], [], "the app's own container says enough")

    def test_on_host(self):
        mounts = [("bind", "/mnt/tank/apps", "/apps"), ("bind", "/mnt/tank/apps/deep", "/apps/deep"),
                  ("volume", "/var/lib/docker/volumes/x/_data", "/data"), ("bind", "/", "/host")]
        self.assertEqual(docker.on_host("/apps/shop", mounts), "/mnt/tank/apps/shop")
        self.assertEqual(docker.on_host("/apps", mounts), "/mnt/tank/apps")
        self.assertEqual(docker.on_host("/apps/deep/x", mounts), "/mnt/tank/apps/deep/x")
        self.assertEqual(docker.on_host("/host/srv/a", mounts), "/srv/a")
        self.assertIsNone(docker.on_host("/data/x", mounts))
        self.assertIsNone(docker.on_host("/appsx", mounts))
        self.assertIsNone(docker.on_host("/elsewhere", mounts))


class Updating(WorldCase):
    def test_update(self):
        old, (old_cid, old_c) = self.head(), self.container()
        new = self.push(app="version 2", message="Second version")
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual((entry["result"], entry["note"]), ("done", ""))
        self.assertEqual(self.head(), new)
        cid, c = self.container()
        self.assertNotEqual(cid, old_cid)
        self.assertNotEqual(c["image"], old_c["image"])
        self.assertEqual(self.tagged("shop-app"), c["image"])
        self.assertEqual(self.tagged(PREVIOUS), old_c["image"], "the old image is kept to go back to")
        self.assertEqual(self.tagged(OUTGOING), "")
        p = self.project()
        self.assertEqual((p["state"], p["behind"]), ("ok", 0))
        self.assertEqual(p["previous"]["sha"], old)
        self.assertEqual((p["history"][0]["from"]["sha"], p["history"][0]["to"]["sha"]), (old, new))
        log = self.mgr.log(entry["id"])
        for line in ("== Getting the new code", "== Building the new image", "== Starting the new version",
                     "== Waiting for it to come up", "== Done."):
            self.assertIn(line, log)
        self.assertIsNone(self.mgr.store.pending())

    def test_build_failure_leaves_the_app_alone(self):
        old, (cid, c) = self.head(), self.container()
        self.push(app="version 2 nobuild")
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn("could not be built", entry["note"])
        self.assertIn("was not touched", entry["note"])
        self.assertEqual(self.head(), old, "the code is moved back")
        self.assertEqual(self.container(), (cid, c), "the very same container is still there")
        self.assertEqual(self.tagged("shop-app"), c["image"])
        self.assertEqual(self.tagged(OUTGOING), "")
        self.assertIn("did not complete successfully", self.mgr.log(entry["id"]))
        self.assertEqual(self.project()["state"], "waiting", "the update is still on offer")

    def test_crash_is_put_back(self):
        old, (_, old_c) = self.head(), self.container()
        self.push(app="version 2 crash")
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "put back")
        self.assertIn("exit code 1", entry["note"])
        self.assertIn("running again", entry["note"])
        self.assertEqual(self.head(), old)
        _, c = self.container()
        self.assertEqual((c["image"], c["status"]), (old_c["image"], "running"))
        self.assertEqual(self.tagged("shop-app"), old_c["image"])
        self.assertEqual(self.tagged(OUTGOING), "")
        self.assertEqual(self.tagged(PREVIOUS), "", "a version that never ran well is not kept as the earlier one")
        log = self.mgr.log(entry["id"])
        self.assertIn("== Putting the old version back", log)
        self.assertIn("the last words of", log)
        p = self.project()
        self.assertEqual(p["state"], "waiting")
        self.assertIsNone(p["previous"])

    def test_failed_health_check_is_put_back(self):
        _, old_c = self.container()
        self.push(app="version 2 unhealthy")
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "put back")
        self.assertIn("its own health check", entry["note"])
        self.assertEqual(self.container()[1]["image"], old_c["image"])

    def test_passing_health_check(self):
        new = self.push(app="version 2 healthy")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        self.assertEqual(self.head(), new)
        self.assertEqual(self.project()["container"]["health"], "healthy")

    def test_never_coming_up_is_put_back(self):
        _, old_c = self.container()
        self.push(app="version 2 slow")
        began = time.monotonic()
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "put back")
        self.assertIn("after 5 seconds it was still waiting for its own health check", entry["note"])
        self.assertGreaterEqual(time.monotonic() - began, 5)
        self.assertEqual(self.container()[1]["image"], old_c["image"])

    def test_container_that_cannot_be_started(self):
        old, (_, old_c) = self.head(), self.container()
        self.push(app="version 2")
        self.change_world(up_fails=True)
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "stuck", "the old one cannot be started either")
        self.assertIn("needs a look by hand", entry["note"])
        self.assertEqual(self.tagged(OUTGOING), old_c["image"], "the image that was running is kept by name")
        self.assertEqual(self.tagged("shop-app"), old_c["image"])
        self.assertEqual(self.head(), old)

    def test_old_version_that_does_not_return(self):
        _, old_c = self.container()
        self.push(app="version 2 crash")
        self.change_world(crash_images=[old_c["image"]])
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "stuck")
        self.assertEqual(self.tagged(OUTGOING), old_c["image"])
        self.assertEqual(self.project()["history"][0]["result"], "stuck")

    def test_same_image_is_not_restarted(self):
        cid, c = self.container()
        new = self.push(readme="A better description.")
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "done")
        self.assertIn("very same image", entry["note"])
        self.assertEqual(self.head(), new)
        self.assertEqual(self.container(), (cid, c))
        self.assertEqual(self.tagged(OUTGOING), "")
        self.assertIsNone(self.project()["previous"])

    def test_nothing_newer(self):
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn("nothing newer", entry["note"])

    def test_something_newer_than_was_shown(self):
        shown = self.push(app="version 2")
        self.assertEqual(self.look()["target"]["sha"], shown)
        self.push(app="version 3")
        old = self.head()
        self.mgr.start("update", "shop", expect=shown)
        deadline = time.monotonic() + 30
        while self.mgr.job and time.monotonic() < deadline:
            time.sleep(0.05)
        entry = self.project()["history"][0]
        self.assertEqual(entry["result"], "failed")
        self.assertIn("Something newer arrived", entry["note"])
        self.assertEqual(self.head(), old)

    def test_one_at_a_time(self):
        self.push(app="version 2 slow")
        self.mgr.start("update", "shop")
        with self.assertRaises(Busy):
            self.mgr.start("update", "shop")
        state = self.mgr.state()
        self.assertEqual(state["busy"], "shop")
        self.assertEqual(state["projects"][0]["job"]["kind"], "update")
        deadline = time.monotonic() + 30
        while self.mgr.job and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(self.mgr.job)

    def test_unknown_project(self):
        with self.assertRaises(KeyError):
            self.mgr.start("update", "nope")
        with self.assertRaises(ValueError):
            self.mgr.start("delete", "shop")
        self.assertIsNone(self.mgr.job)

    def test_interrupted_attempt_is_told(self):
        self.mgr.store.set_pending({"id": "shop-20260101-000000", "kind": "update", "name": "shop",
                                    "at": 1767225600, "step": "build"})
        again = Manager(self.home)
        entry = again.store.history("shop")[0]
        self.assertEqual(entry["result"], "interrupted")
        self.assertIn("Building the new image", entry["note"])
        self.assertIsNone(again.store.pending())


class AsItWasStarted(WorldCase):
    """Pulley has to run Compose exactly as the app was started, or the app comes back different."""

    def prepare(self):
        # An override file of the person's own, which git does not track, and a home folder.
        (self.work / "compose.override.yaml").write_text(
            json.dumps({"services": {"app": {"volumes": ["./data:/data", "~/shop-keys:/keys"]}}}), "utf-8")
        self.world_file.write_text(json.dumps({"home": "/home/someone"}), "utf-8")

    def mounts(self):
        return [["bind", self.folder("data"), "/data"], ["bind", "/home/someone/shop-keys", "/keys"]]

    def test_override_file_is_kept(self):
        self.assertEqual(self.container()[1]["mounts"], self.mounts())
        new = self.push(app="version 2")
        self.assertEqual(self.look()["problems"], [])
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        _, c = self.container()
        self.assertEqual(c["mounts"], self.mounts(), "the app kept its folders")
        self.assertEqual(len(c["labels"]["com.docker.compose.project.config_files"].split(",")), 2)
        self.assertEqual(self.head(), new)

    def test_override_file_is_kept_when_put_back(self):
        self.push(app="version 2 crash")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "put back")
        self.assertEqual(self.container()[1]["mounts"], self.mounts())

    def test_home_folder_that_means_something_else_to_pulley(self):
        cid = self.container()[0]
        self.change_world(home="/root")  # what ~ is inside Pulley's container
        self.push(app="version 2")
        p = self.look()
        self.assertEqual(p["state"], "blocked")
        self.assertIn("would get /root/shop-keys as its /keys", p["problems"][0])
        self.assertIn("has /home/someone/shop-keys there", p["problems"][0])
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "failed")
        self.assertEqual(self.container()[0], cid)

    def test_folder_check_can_be_switched_off(self):
        self.settings("skip_path_check = true")
        self.change_world(home="/root")
        self.assertEqual(self.look()["problems"], [])

    def test_compose_file_out_of_sight(self):
        cid, c = self.container()
        labels = {**c["labels"], "com.docker.compose.project.config_files": "/mnt/tank/apps/shop/compose.yaml"}
        self.change_world(containers={cid: {**c, "labels": labels}})
        self.assertIn("was started from /mnt/tank/apps/shop/compose.yaml, which Pulley cannot see",
                      self.look()["problems"][0])

    def test_container_that_says_nothing_about_its_start(self):
        cid, c = self.container()
        labels = {k: v for k, v in c["labels"].items() if "config_files" not in k}
        self.change_world(containers={cid: {**c, "labels": labels}})
        self.assertIn("does not say which compose file", self.look()["problems"][0])

    def test_leftover_run_container_does_not_count(self):
        cid, c = self.container()
        stray = {**c, "status": "exited", "labels": {**c["labels"], "com.docker.compose.oneoff": "True"}}
        self.change_world(containers={"f" * 64: stray, cid: c})
        self.push(app="version 2")
        self.assertEqual(self.look()["state"], "waiting")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        self.assertIn("f" * 64, self.world()["containers"], "and it is left where it was")


class ComposeFileChanges(WorldCase):
    """The compose file is part of the repository, so it can change with the code."""

    QUIET = {"services": {"app": {"build": ".", "command": "serve --quiet"}}}

    def test_put_back_starts_the_old_image_the_old_way(self):
        old = self.head()
        self.push(app="version 2 crash", compose=self.QUIET)
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "put back")
        _, c = self.container()
        self.assertEqual((c["command"], c["status"]), ("", "running"))
        self.assertEqual(self.head(), old)

    def test_change_to_the_compose_file_alone_is_applied(self):
        cid, c = self.container()
        self.push(compose=self.QUIET)
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual((entry["result"], entry["note"]), ("done", ""))
        new_cid, new_c = self.container()
        self.assertNotEqual(new_cid, cid)
        self.assertEqual((new_c["image"], new_c["command"]), (c["image"], "serve --quiet"))
        self.assertEqual(self.mgr.run_now("back", "shop")["result"], "done", "and it can be taken back")
        self.assertEqual(self.container()[1]["command"], "")

    def test_new_version_wanting_a_value_pulley_lacks(self):
        old, (cid, _) = self.head(), self.container()
        self.push(app="version 2", compose={"services": {"app": {"build": ".", "command": "serve ${NEW_KEY}"}}})
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn("uses NEW_KEY, which Pulley has no value for", entry["note"])
        self.assertIn("was not touched", entry["note"])
        self.assertEqual((self.head(), self.container()[0]), (old, cid))
        self.assertEqual(self.tagged(OUTGOING), "")

    def test_new_version_without_the_service(self):
        old, (cid, _) = self.head(), self.container()
        self.push(app="version 2", compose={"services": {"web": {"build": "."}}})
        entry = self.mgr.run_now("update", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn('does not build a service called "app"', entry["note"])
        self.assertEqual((self.head(), self.container()[0]), (old, cid))


class NamedImage(WorldCase):
    """A compose file that names its image, and a new version that names it differently."""

    compose = {"services": {"app": {"build": ".", "image": "shop:1.0"}}}
    NEWER = {"services": {"app": {"build": ".", "image": "shop:1.1"}}}

    def test_new_name_is_followed_there_and_back(self):
        old, (_, old_c) = self.head(), self.container()
        self.push(app="version 2", compose=self.NEWER)
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        _, c = self.container()
        self.assertNotEqual(c["image"], old_c["image"])
        self.assertEqual((c["ref"], self.tagged("shop:1.1")), ("shop:1.1", c["image"]))
        self.assertEqual(self.tagged(PREVIOUS), old_c["image"])
        self.assertEqual(self.mgr.run_now("back", "shop")["result"], "done")
        _, c = self.container()
        self.assertEqual((c["ref"], c["image"], self.head()), ("shop:1.0", old_c["image"], old))

    def test_new_name_that_fails_is_put_back_under_the_old_one(self):
        old, (_, old_c) = self.head(), self.container()
        self.push(app="version 2 crash", compose=self.NEWER)
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "put back")
        _, c = self.container()
        self.assertEqual((c["ref"], c["image"], c["status"], self.head()),
                         ("shop:1.0", old_c["image"], "running", old))

    def test_same_code_under_a_new_name_is_still_restarted(self):
        cid, c = self.container()
        self.push(compose=self.NEWER)
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        new_cid, new_c = self.container()
        self.assertNotEqual(new_cid, cid)
        self.assertEqual((new_c["ref"], new_c["image"]), ("shop:1.1", c["image"]))


class TwoPulleys(WorldCase):
    """The command line used beside the running page: two Pulleys, one data folder."""

    def test_they_do_not_overwrite_each_other(self):
        self.push(app="version 2")
        other = Manager(self.home)
        other.poll, other.own = 0.02, lambda: None
        self.assertEqual(other.run_now("update", "shop")["result"], "done")
        self.mgr.store.set_pending(None)  # the first one saves something of its own
        p = self.project()
        self.assertEqual(p["history"][0]["result"], "done")
        self.assertIsNotNone(p["previous"])
        self.assertIsNotNone(Manager(self.home).store.previous("shop"), "and it is still on disk")

    def test_the_page_notices_what_the_command_line_did(self):
        new = self.push(app="version 2")
        self.assertEqual(self.look()["state"], "waiting")
        other = Manager(self.home)
        other.poll, other.own = 0.02, lambda: None
        self.assertEqual(other.run_now("update", "shop")["result"], "done")
        self.assertEqual(self.project()["history"][0]["result"], "done", "asking is what makes it look again")
        deadline = time.monotonic() + 30
        while self.mgr.checking and time.monotonic() < deadline:
            time.sleep(0.05)
        p = self.project()
        self.assertEqual((p["state"], p["current"]["sha"]), ("ok", new))
        self.assertFalse(self.mgr.checking, "and it does not keep looking")

    def test_one_at_a_time_between_them(self):
        self.push(app="version 2")
        self.mgr.store.set_pending({"id": "shop-elsewhere", "kind": "update", "name": "shop", "at": 1,
                                    "step": "build", "pid": 4242})
        real = engine._alive
        self.addCleanup(setattr, engine, "_alive", real)
        engine._alive = lambda pid: pid == 4242
        with self.assertRaises(Busy) as caught:
            self.mgr.start("update", "shop")
        self.assertIn("another Pulley", str(caught.exception))
        started = Manager(self.home)  # starting up must not call the other one's work interrupted
        self.assertEqual(started.store.history("shop"), [])
        self.assertIsNotNone(started.store.pending())
        engine._alive = lambda pid: False  # the other one is gone
        self.assertEqual(Manager(self.home).store.history("shop")[0]["result"], "interrupted")


class GoingBack(WorldCase):
    def updated(self):
        """Take the shop from version 1 to version 2. Returns (first commit, first image, second commit)."""
        first, (_, c) = self.head(), self.container()
        second = self.push(app="version 2")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        return first, c["image"], second

    def test_go_back(self):
        first, first_image, second = self.updated()
        entry = self.mgr.run_now("back", "shop")
        self.assertEqual((entry["result"], entry["note"]), ("done", ""))
        self.assertEqual(self.head(), first)
        _, c = self.container()
        self.assertEqual((c["image"], c["status"]), (first_image, "running"))
        self.assertEqual(self.tagged("shop-app"), first_image)
        self.assertEqual((self.tagged(PREVIOUS), self.tagged(OUTGOING)), ("", ""))
        p = self.project()
        self.assertIsNone(p["previous"])
        self.assertEqual((p["state"], p["behind"]), ("waiting", 1), "the newer version is on offer again")
        self.assertEqual(p["history"][0]["kind"], "back")
        self.assertIn("== Putting the earlier code in place", self.mgr.log(entry["id"]))
        # ...and forward again
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        self.assertEqual(self.head(), second)

    def test_going_back_to_something_broken_returns(self):
        first, first_image, second = self.updated()
        _, c = self.container()
        self.change_world(crash_images=[first_image])
        entry = self.mgr.run_now("back", "shop")
        self.assertEqual(entry["result"], "put back")
        self.assertEqual(self.head(), second)
        self.assertEqual(self.container()[1]["image"], c["image"])
        self.assertEqual(self.tagged("shop-app"), c["image"])
        self.assertEqual(self.tagged(PREVIOUS), first_image, "the earlier version is still kept")
        self.assertIn("== Returning to the version that was running", self.mgr.log(entry["id"]))

    def test_nothing_kept(self):
        entry = self.mgr.run_now("back", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn("No earlier version is kept", entry["note"])

    def test_kept_image_gone(self):
        self.updated()
        self.docker("rmi", PREVIOUS)
        entry = self.mgr.run_now("back", "shop")
        self.assertEqual(entry["result"], "failed")
        self.assertIn("no longer on this machine", entry["note"])
        self.assertIsNone(self.project()["previous"])

    def test_failed_update_keeps_the_earlier_version(self):
        first, first_image, second = self.updated()
        self.push(app="version 3 crash")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "put back")
        self.assertEqual(self.head(), second)
        self.assertEqual(self.tagged(PREVIOUS), first_image)
        self.assertEqual(self.project()["previous"]["sha"], first)


class Tags(WorldCase):
    more = 'follow = "tags"'

    def test_no_tags(self):
        self.push(app="version 2")
        p = self.look()
        self.assertIn("no version tags", p["problems"][0])
        self.assertEqual(p["behind"], 0)

    def test_follows_the_newest_version(self):
        self.push(app="version 2", tag="v1.1.0")
        wanted = self.push(app="version 3", tag="v1.10.0")
        self.push(app="version 4", tag="v1.9.0")
        self.push(app="version 5", message="Not released yet")
        p = self.look()
        self.assertEqual(p["follows"], "version tags")
        self.assertEqual((p["target"]["sha"], p["target"]["tag"]), (wanted, "v1.10.0"))
        self.assertEqual(p["behind"], 2)
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        self.assertEqual(self.head(), wanted)
        p = self.project()
        self.assertEqual((p["state"], p["current"]["tag"]), ("ok", "v1.10.0"))

    def test_leaves_release_candidates_alone(self):
        wanted = self.push(app="version 2", tag="v1.1.0")
        self.push(app="version 3", tag="v1.2.0-rc1")
        self.push(app="version 4", tag="v2.0.0-beta.2")
        self.assertEqual(self.look()["target"]["sha"], wanted)

    def test_release_candidates_when_asked(self):
        self.settings('follow = "tags"\nprereleases = true')
        self.push(app="version 2", tag="v1.1.0")
        wanted = self.push(app="version 3", tag="v1.2.0-rc1")
        self.assertEqual(self.look()["target"]["sha"], wanted)

    def test_final_release_beats_its_candidate(self):
        self.settings('follow = "tags"\nprereleases = true')
        self.push(app="version 2", tag="v1.2.0-rc1")
        wanted = self.push(app="version 3", tag="v1.2.0")
        self.assertEqual(self.look()["target"]["sha"], wanted)

    def test_only_matching_tags(self):
        self.settings('follow = "tags"\ntags = "v*"')
        wanted = self.push(app="version 2", tag="v1.1.0")
        self.push(app="version 3", tag="nightly-20260101")
        self.assertEqual(self.look()["target"]["sha"], wanted)

    def test_newer_than_the_newest_version(self):
        self.push(app="version 2", tag="v1.1.0")
        git(self.work, "pull", "-q", "--ff-only")
        self.push(app="version 3")
        git(self.work, "pull", "-q", "--ff-only")
        p = self.look()
        self.assertEqual((p["state"], p["behind"]), ("ok", 0))
        self.assertIn("newer than what it follows", p["notes"][0])


class Detached(WorldCase):
    """A checkout that sits on a bare commit rather than a branch."""

    more = 'branch = "main"'

    def test_update_and_back(self):
        first = self.head()
        git(self.work, "checkout", "-q", "--detach")
        second = self.push(app="version 2")
        self.assertEqual(self.look()["state"], "waiting")
        self.assertEqual(self.mgr.run_now("update", "shop")["result"], "done")
        self.assertEqual(self.head(), second)
        self.assertEqual(self.mgr.run_now("back", "shop")["result"], "done")
        self.assertEqual(self.head(), first)

    def test_needs_to_be_told_the_branch(self):
        self.settings()
        git(self.work, "checkout", "-q", "--detach")
        self.assertIn("not on a branch", self.look()["problems"][0])


if __name__ == "__main__":
    unittest.main()
