from __future__ import annotations

from engine.director import DirectorHandle, NarrativeDirector


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
            node.director.direct(node_handle, current)
            previous_text = node_handle.take_final_text() or previous_text
            if index < len(self.nodes) - 1:
                current = _with_handoff(current, node, previous_text)
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

    def report_model_call_started(self, meta):
        self._parent.report_model_call_started({**meta, "agent_node_id": self.node.id, "agent_role": self.node.role})

    def report_model_call_finished(self, meta):
        self._parent.report_model_call_finished({**meta, "agent_node_id": self.node.id, "agent_role": self.node.role})


def _with_handoff(compiled, node, text):
    payload = list(compiled.payload)
    payload.append(
        {
            "role": "user",
            "content": (
                f"[sequential graph handoff from {node.id}/{node.role}]\n"
                f"{text}"
            ),
        }
    )
    return type(compiled)(payload, compiled.manifest, compiled.payload_hash, compiled.stable_payload_hash)
