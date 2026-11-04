"""Security policy rules generated from the contract, and policies compared with it.

Both policy files in demo/cockpit/secpol compile with secpolcompile from
QNX SDP 8.0; these tests only need Python.
"""

import os
import unittest

from blueprint import checks, contract, secpol

HERE = os.path.dirname(__file__)
DEMO = os.path.join(HERE, "..", "demo", "cockpit")
CONTRACT = contract.load(os.path.join(DEMO, "blueprint.yaml"))


def read(name):
    with open(os.path.join(DEMO, "secpol", name), encoding="utf-8") as f:
        return f.read()


def errors(findings):
    return sorted(f.title for f in findings if f.severity == checks.ERROR)


class Generate(unittest.TestCase):
    def test_one_connect_rule_per_contract_edge(self):
        text = secpol.generate(CONTRACT)
        pol = secpol.parse(text)
        self.assertEqual(pol.connect, {(f"{a}_t", f"{b}_t") for a, b in CONTRACT.allow})
        self.assertEqual(pol.types, {f"{p}_t" for p in CONTRACT.processes})

    def test_attach_and_type_overrides(self):
        text = secpol.generate(CONTRACT, {"nav": ["/dev/name/local/cockpit-nav"]}, {"display": "hmi_t"})
        self.assertIn("allow_attach nav_t /dev/name/local/cockpit-nav;", text)
        self.assertIn("allow hmi_t nav_t:channel connect;", text)
        self.assertNotIn("display_t", text.split("\n", 6)[-1])

    def test_unknown_process_is_refused(self):
        with self.assertRaises(secpol.PolicyError):
            secpol.generate(CONTRACT, {"radar": ["/dev/radar"]})
        with self.assertRaises(secpol.PolicyError):
            secpol.types_for(CONTRACT, {"radar": "radar_t"})

    def test_checked_in_policy_is_current(self):
        attach = {n: [f"/dev/name/local/cockpit-{n}"] for n in ("gps", "nav", "camera", "logger")}
        self.assertEqual(read("cockpit.secpol"), secpol.generate(CONTRACT, attach, source="blueprint.yaml"))

    def test_type_names(self):
        self.assertEqual(secpol.type_name("io-sock"), "io_sock_t")
        self.assertEqual(secpol.type_name("3d"), "p_3d_t")


class Compare(unittest.TestCase):
    def test_generated_policy_matches(self):
        self.assertEqual(secpol.compare(CONTRACT, secpol.parse(read("cockpit.secpol"))), [])

    def test_backdoor_rule_through_an_attribute(self):
        findings = secpol.compare(CONTRACT, secpol.parse(read("backdoor.secpol")), "backdoor.secpol")
        self.assertEqual(errors(findings), ["policy lets display connect to gps, which the contract forbids"])
        self.assertEqual(findings[0].data["line"], 7)

    def test_missing_rule_would_block_an_allowed_edge(self):
        text = read("cockpit.secpol").replace("allow display_t camera_t:channel connect;", "")
        self.assertEqual(errors(secpol.compare(CONTRACT, secpol.parse(text))),
                         ["policy blocks display -> camera, which the contract allows"])

    def test_ability_form_and_sets(self):
        text = """type a_t; type b_t; type c_t;
                  allow { a_t b_t } c_t : channel connect;
                  allow c_t self:ability { nonroot channel_connect:a_t };"""
        self.assertEqual(secpol.parse(text).connect, {("a_t", "c_t"), ("b_t", "c_t"), ("c_t", "a_t")})

    def test_missing_types_warn(self):
        findings = secpol.compare(CONTRACT, secpol.parse("type nav_t;"))
        self.assertEqual(findings[0].severity, checks.WARNING)
        self.assertIn("gps", findings[0].title)

    def test_comments_and_unterminated_statement(self):
        self.assertEqual(secpol.parse("# allow a b:channel connect;\ntype a_t; # x;\n").types, {"a_t"})
        with self.assertRaises(secpol.PolicyError):
            secpol.parse("type a_t")



class LearnedOnQnx(unittest.TestCase):
    """Policies that secpolgenerate wrote on QNX 8.0 while the cockpit ran."""

    def test_learned_from_the_designed_system_matches(self):
        pol = secpol.parse(read("learned-ok.secpol"))
        self.assertGreater(len(pol.types), 30)        # the whole system, not only the cockpit
        self.assertEqual(secpol.compare(CONTRACT, pol), [])

    def test_one_backdoor_run_is_enough_to_widen_the_policy(self):
        findings = secpol.compare(CONTRACT, secpol.parse(read("learned-backdoor.secpol")))
        self.assertEqual(errors(findings), ["policy lets display connect to gps, which the contract forbids"])


class FailClosed(unittest.TestCase):
    """A rule the parser cannot read must never turn into a pass."""

    def test_bare_channel_connect_means_every_type(self):
        text = read("cockpit.secpol") + "allow display_t self:ability { nonroot channel_connect };\n"
        self.assertIn("policy lets display connect to gps, which the contract forbids",
                      errors(secpol.compare(CONTRACT, secpol.parse(text))))

    def test_spaces_in_ranges(self):
        text = "type a_t; type b_t; type c_t;\nallow a_t self:ability {\n    channel_connect: b_t , c_t\n};"
        self.assertEqual(secpol.parse(text).connect, {("a_t", "b_t"), ("a_t", "c_t")})

    def test_ability_with_named_object(self):
        self.assertEqual(secpol.parse("type a_t; type b_t; allow a_t a_t:ability { channel_connect:b_t };").connect,
                         {("a_t", "b_t")})

    def test_default_rules_cover_every_type(self):
        text = read("cockpit.secpol") + "allow default_rules self:ability { channel_connect:gps_t };\n"
        self.assertIn("policy lets display connect to gps, which the contract forbids",
                      errors(secpol.compare(CONTRACT, secpol.parse(text))))

    def test_unreadable_channel_rules_are_errors(self):
        for rule in ["allow display_t gps_t channel connect;",
                     "allow display_t self:ability { channel_connect:gps_t/x };",
                     "allow display_t gps_t:file { read channel_connect };"]:
            with self.assertRaises(secpol.PolicyError, msg=rule):
                secpol.parse("type display_t; type gps_t;\n" + rule)

    def test_commas_in_type_sets(self):
        text = "type a_t; type b_t; type c_t;\nallow { a_t, b_t } { c_t , a_t }:channel connect;"
        self.assertEqual(secpol.parse(text).connect,
                         {("a_t", "c_t"), ("a_t", "a_t"), ("b_t", "c_t"), ("b_t", "a_t")})

    def test_wildcards_and_complements_are_errors(self):
        for rule in ["allow display_t gps_t:channel *;", "allow display_t gps_t:channel ~connect;",
                     "allow display_t * :channel connect;", "allow display_t self:ability { * };",
                     "allow display_t self:ability { ~channel_connect:gps_t };",
                     "allow { display_t; gps_t }:channel connect;"]:
            with self.assertRaises(secpol.PolicyError, msg=rule):
                secpol.parse("type display_t; type gps_t;\n" + rule)

    def test_other_rules_are_still_ignored(self):
        pol = secpol.parse("type a_t; allow a_t self:ability { nonroot spawn mem_phys:0x1000-0x2000 };\n"
                           "allow_attach a_t /dev/x; derive_type a_t run b_t;")
        self.assertEqual(pol.connect, set())


if __name__ == "__main__":
    unittest.main()
