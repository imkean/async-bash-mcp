"""Tests for the local fork's additions (see module docstring "[fork]" notes)."""

import asyncio
import os
import time

import pytest

import async_bash_mcp as abm


@pytest.fixture
def process_manager():
    return abm.ProcessManager()


@pytest.mark.asyncio
async def test_default_cwd_from_env(process_manager, monkeypatch, tmp_path):
    monkeypatch.setattr(abm, "DEFAULT_CWD", str(tmp_path))
    process_id = await process_manager.spawn_process("pwd")

    await asyncio.sleep(0.2)
    result = await process_manager.poll_process(process_id, wait_ms=100)

    assert str(tmp_path) in result["stdout"]


@pytest.mark.asyncio
async def test_explicit_cwd_wins_over_default(process_manager, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(abm, "DEFAULT_CWD", str(tmp_path))
    process_id = await process_manager.spawn_process("pwd", cwd=str(other))

    await asyncio.sleep(0.2)
    result = await process_manager.poll_process(process_id, wait_ms=100)

    assert str(other) in result["stdout"]


@pytest.mark.asyncio
async def test_shell_override_enables_bash_word_splitting(
    process_manager, monkeypatch
):
    monkeypatch.setattr(abm, "SHELL", "/bin/bash")
    process_id = await process_manager.spawn_process(
        "L='a b'; for x in $L; do echo \"[$x]\"; done"
    )

    await asyncio.sleep(0.2)
    result = await process_manager.poll_process(process_id, wait_ms=100)

    assert result["stdout"].split() == ["[a]", "[b]"]


@pytest.mark.asyncio
async def test_output_buffer_is_bounded_and_announced(process_manager, monkeypatch):
    monkeypatch.setattr(abm, "MAX_OUTPUT_CHARS", 100, raising=True)
    process_id = await process_manager.spawn_process(
        "python3 -c \"print('x' * 5000)\""
    )

    await asyncio.sleep(0.3)
    result = await process_manager.poll_process(process_id, wait_ms=200)

    assert "discarded" in result["stdout"]
    assert len(result["stdout"]) < 5000

    # The marker is reported once, not on every poll.
    second = await process_manager.poll_process(process_id, wait_ms=100)
    assert "discarded" not in second["stdout"]


@pytest.mark.asyncio
async def test_terminate_kills_the_whole_process_group(process_manager):
    # The grandchild inherits the pipe; without a process-group kill it would
    # survive and keep the read tasks blocked.
    process_id = await process_manager.spawn_process("sleep 30 & echo $!; wait")

    await asyncio.sleep(0.3)
    result = await process_manager.poll_process(process_id, wait_ms=200)
    grandchild = int(result["stdout"].split()[0])

    terminated = await process_manager.poll_process(
        process_id, wait_ms=100, terminate=True
    )
    assert terminated["finished"] is True

    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


@pytest.mark.asyncio
async def test_reap_kills_unpolled_process(process_manager, monkeypatch):
    monkeypatch.setattr(abm, "IDLE_TIMEOUT", 10)
    process_id = await process_manager.spawn_process("sleep 30")
    await asyncio.sleep(0.2)

    with process_manager._lock:
        process_manager._processes[process_id].last_poll = time.time() - 60

    await process_manager.reap()
    await asyncio.sleep(0.2)

    result = await process_manager.poll_process(process_id, wait_ms=100)
    assert result["finished"] is True
    assert "without a poll" in result["stderr"]


@pytest.mark.asyncio
async def test_reap_prunes_stale_finished_entries(process_manager, monkeypatch):
    monkeypatch.setattr(abm, "FINISHED_RETENTION", 1)
    process_id = await process_manager.spawn_process("echo hi")
    await asyncio.sleep(0.3)

    with process_manager._lock:
        process_manager._processes[process_id].last_poll = time.time() - 60

    await process_manager.reap()

    with process_manager._lock:
        assert process_id not in process_manager._processes


def test_process_ids_do_not_restart_from_one():
    assert abm.ProcessManager._global_next_id > 1_000_000
