from octopus import host


def test_system_names_the_platform():
    assert host.system_of("darwin") == host.MAC
    assert host.system_of("linux") == host.LINUX
    assert host.system_of("win32") == host.OTHER


def test_tests_run_as_mac_unless_a_test_says_otherwise():
    assert host.system() == host.MAC


def test_in_container():
    assert host.in_container(exists=lambda p: p == "/.dockerenv", environ={}) is True
    assert host.in_container(exists=lambda p: p == "/run/.containerenv", environ={}) is True
    assert host.in_container(exists=lambda p: False, environ={"container": "podman"}) is True
    assert host.in_container(exists=lambda p: False, environ={}) is False
