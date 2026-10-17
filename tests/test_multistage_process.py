"""Exercise process and step lifecycle transitions with deterministic worker scheduling."""
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if 'catalystlab' not in sys.modules:
    package = types.ModuleType('catalystlab')
    package.__path__ = [str(ROOT/'src')] + [str(path) for path in (ROOT/'src').rglob('*') if path.is_dir()]
    sys.modules['catalystlab'] = package

from catalystlab import multistage_process as process_module


Process = process_module.MultiStageProcess
ProcessState = process_module.MultiStageProcessState
Stage = process_module.MultiStageProcessStage
StageEvent = process_module.MultiStageProcessStageEvent
StageState = process_module.MultiStageProcessStageState


class ControlledStage(Stage):
    def __init__(self, name, process, result=StageState.COMPLETED):
        super().__init__(name, f'{name} description', process)
        self.result = result
        self.cleanup_called = False

    def start(self):
        super().start()
        self._update_progress(0.5)
        self.complete(self.result)

    def cleanup(self):
        self.cleanup_called = True
        return super().cleanup()


class ControlledProcess(Process):
    def __init__(self, results):
        self.results = results
        self.completions = []
        super().__init__('test process')

    def setup_stages(self):
        self.stages = [ControlledStage(f'step {index}', self, result)
                       for index, result in enumerate(self.results)]

    def complete_process(self, success):
        self.completions.append(success)


class InlineThread:
    """Run worker targets inline to make the queue deterministic in unit tests."""
    def __init__(self, target):
        self.target = target

    def start(self):
        self.target()


class MultiStageProcessTests(unittest.TestCase):
    def setUp(self):
        self.started = Process.started_processes
        self.finished = Process.finished_processes
        self.started.clear()
        self.finished.clear()
        self.addCleanup(self.started.clear)
        self.addCleanup(self.finished.clear)
        self.scheduler = patch.object(process_module.threading, 'Thread', InlineThread)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.idle = patch.object(process_module.GLib, 'idle_add', side_effect=lambda callback, *args: callback(*args))
        self.idle.start()
        self.addCleanup(self.idle.stop)

    def test_steps_run_in_order_and_success_moves_to_finished_until_seen(self):
        process = ControlledProcess([StageState.COMPLETED, StageState.COMPLETED])
        process.start()
        self.assertEqual([stage.state for stage in process.stages], [StageState.COMPLETED, StageState.COMPLETED])
        self.assertEqual(process.status, ProcessState.COMPLETED)
        self.assertEqual(process.completions, [True])
        self.assertNotIn(process, Process.started_processes)
        self.assertIn(process, Process.finished_processes)
        process.mark_result_seen()
        self.assertNotIn(process, Process.finished_processes)
        self.assertTrue(process.result_seen)

    def test_failed_step_stops_queue_and_completes_unsuccessfully(self):
        process = ControlledProcess([StageState.FAILED, StageState.SCHEDULED])
        process.start()
        self.assertEqual(process.status, ProcessState.FAILED)
        self.assertEqual(process.stages[1].state, StageState.SCHEDULED)
        self.assertEqual(process.completions, [False])
        self.assertNotIn(process, Process.started_processes)
        self.assertNotIn(process, Process.finished_processes)

    def test_cancel_before_start_resets_to_setup_and_completes_as_failure(self):
        process = ControlledProcess([StageState.SCHEDULED])
        process.cancel()
        self.assertEqual(process.status, ProcessState.SETUP)
        self.assertEqual(process.completions, [False])
        self.assertEqual(process.stages[0].state, StageState.SCHEDULED)

    def test_cancel_during_stage_marks_it_failed_and_stops_process(self):
        process = ControlledProcess([StageState.SCHEDULED])
        stage = process.stages[0]
        stage._update_state(StageState.IN_PROGRESS)
        process.status = ProcessState.IN_PROGRESS
        Process.started_processes.append(process)
        process.cancel()
        self.assertEqual(process.status, ProcessState.FAILED)
        self.assertEqual(stage.state, StageState.FAILED)
        self.assertTrue(stage._cancel_event.is_set())
        self.assertEqual(process.completions, [False])
        self.assertNotIn(process, Process.started_processes)

    def test_progress_is_average_of_stage_progress(self):
        process = ControlledProcess([StageState.SCHEDULED, StageState.SCHEDULED])
        process.stages[0].progress = 0.25
        process.stages[1].progress = 0.75
        process._update_progress(None)
        self.assertEqual(process.progress, 0.5)

    def test_stage_logging_emits_line_and_retains_output(self):
        process = ControlledProcess([StageState.SCHEDULED])
        stage = process.stages[0]
        stage.event_bus.scheduler = lambda callback, *args: callback(*args)
        received = []
        def collect(line):
            received.append(line)
        stage.event_bus.subscribe(StageEvent.OUTPUT_LINE_ADDED, collect)
        stage.log('line one')
        self.assertEqual(stage.output_lines, ['line one'])
        self.assertEqual(received, ['line one'])

    def test_stage_start_clears_previous_server_call(self):
        process = ControlledProcess([StageState.SCHEDULED])
        stage = process.stages[0]
        stage._server_call = object()
        stage.start()
        self.assertIsNone(stage.server_call)

    def test_stage_cancel_cancels_and_joins_server_call(self):
        process = ControlledProcess([StageState.SCHEDULED])
        stage = process.stages[0]
        call = Mock()
        call.thread = Mock()
        stage._server_call = call
        stage._update_state(StageState.IN_PROGRESS)

        stage.cancel()

        call.cancel.assert_called_once()
        call.thread.join.assert_called_once()
        self.assertIsNone(stage.server_call)

    def test_toolset_command_forwards_progress_and_reports_success(self):
        from catalystlab.root_helper_server import ServerResponseStatusCode

        class Call:
            output = []
            output_lock = threading.Lock()
            event_bus = Mock()
            thread = Mock()

        def run_command(**kwargs):
            kwargs['handler']('halfway')
            kwargs['completion_handler'](types.SimpleNamespace(code=ServerResponseStatusCode.OK))
            return Call()

        process = ControlledProcess([StageState.SCHEDULED])
        process.toolset = types.SimpleNamespace(run_command=run_command)
        stage = process.stages[0]
        self.assertTrue(stage.run_command_in_toolset('emerge app', lambda _: 0.5))
        self.assertEqual(stage.progress, 0.5)
        self.assertIsNone(stage.server_call)

    def test_toolset_command_failure_and_exception_are_reported(self):
        from catalystlab.root_helper_server import ServerResponseStatusCode

        class Call:
            output = []
            output_lock = threading.Lock()
            event_bus = Mock()
            thread = Mock()

        def failed_command(**kwargs):
            kwargs['completion_handler'](types.SimpleNamespace(code=ServerResponseStatusCode.COMMAND_EXECUTION_FAILED))
            return Call()

        process = ControlledProcess([StageState.SCHEDULED])
        process.toolset = types.SimpleNamespace(run_command=failed_command)
        self.assertFalse(process.stages[0].run_command_in_toolset('emerge app'))

        process.toolset.run_command = Mock(side_effect=RuntimeError('toolset unavailable'))
        stage = process.stages[0]
        self.assertFalse(stage.run_command_in_toolset('emerge app'))
        self.assertEqual(stage.state, StageState.FAILED)
        self.assertIn('Error: toolset unavailable', stage.output_lines)

    def test_stage_cleanup_skips_scheduled_and_cancels_running(self):
        process = ControlledProcess([StageState.SCHEDULED])
        stage = process.stages[0]
        self.assertFalse(stage.cleanup())
        stage._update_state(StageState.IN_PROGRESS)
        self.assertTrue(stage.cleanup())
        self.assertEqual(stage.state, StageState.FAILED)

    def test_started_processes_are_filtered_by_subclass(self):
        class OtherProcess(ControlledProcess):
            pass

        first, second = ControlledProcess([]), OtherProcess([])
        Process.started_processes.extend([first, second])
        self.assertEqual(Process.get_started_processes_by_class(ControlledProcess), [first, second])
        self.assertEqual(Process.get_started_processes_by_class(OtherProcess), [second])


if __name__ == '__main__':
    unittest.main()
