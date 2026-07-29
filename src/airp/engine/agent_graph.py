from __future__ import annotations

from airp.engine.director import DirectorHandle, NarrativeDirector


class SequentialAgentGraph(NarrativeDirector):
    """Restricted ordered writing-role graph.

    Nodes run one after another and share only the previous node's final text.
    There are no branches, conditions, loops or arbitrary DSL evaluation. The
    final enabled node supplies the draft consumed by the runtime harness.
    """

    def __init__(self, nodes):
        self.nodes = tuple(nodes)
        if not self.nodes:
            raise ValueError("sequential graph requires at least one node")

    def direct(self, handle: DirectorHandle, compiled) -> None:
        current = compiled
        previous_text = ""
        commit_feedback = handle.commit_feedback
        if commit_feedback and len(self.nodes) > 1:
            return self.nodes[-1].director.direct(handle, compiled)
        for index, node in enumerate(self.nodes):
            node_handle = _GraphNodeHandle(handle, node)
            node_handle.report_agent_node_started(node.id, node.role)
            try:
                node.director.direct(node_handle, current)
            finally:
                node_handle.report_agent_node_finished(node.id, node.role)
            previous_text = node_handle.take_final_text() or previous_text
            if index < len(self.nodes) - 1:
                current = node_handle.compile_sequential_handoff(current, node, self.nodes[index + 1], previous_text)
        handle.set_final_text(previous_text)


class SequentialGraphNode:
    def __init__(self, node_id, role, director):
        self.id = node_id
        self.role = role
        self.director = director


class _GraphNodeHandle:
    def __init__(self, parent: DirectorHandle, node: SequentialGraphNode):
        self._parent = parent
        self.node = node
        self._final_text = None

    @property
    def task_id(self):
        return self._parent.task_id

    @property
    def task_text(self):
        return self._parent.task_text

    def call_tool(self, name, args):
        return self._parent.call_tool(name, args)

    def tool_schemas(self):
        return self._parent.tool_schemas()

    def set_final_text(self, text):
        self._final_text = text if text is not None else ""

    def take_final_text(self):
        text = self._final_text
        self._final_text = None
        return text

    def set_commit_feedback(self, error, details=None):
        self._parent.set_commit_feedback(error, details)

    @property
    def commit_feedback(self):
        return self._parent.commit_feedback

    def emit_preview(self, text):
        self._parent.emit_preview(text)

    @property
    def aborted(self):
        return self._parent.aborted

    @property
    def signal(self):
        return self._parent.signal

    def compile_follow_up(self):
        return self._parent.compile_follow_up()

    def compile_sequential_handoff(self, compiled, source_node, target_node, text):
        return self._parent.compile_sequential_handoff(compiled, source_node, target_node, text)

    def report_agent_node_started(self, node_id, role):
        getattr(self._parent, "report_agent_node_started", lambda _id, _role: None)(node_id, role)

    def report_agent_node_finished(self, node_id, role):
        getattr(self._parent, "report_agent_node_finished", lambda _id, _role: None)(node_id, role)

    def report_model_call_started(self, meta):
        self._parent.report_model_call_started({**meta, "agent_node_id": self.node.id, "agent_role": self.node.role})

    def report_model_call_finished(self, meta):
        self._parent.report_model_call_finished({**meta, "agent_node_id": self.node.id, "agent_role": self.node.role})
