"""Default-profile responsiveness benchmark.

Measures how quickly a user on the DEFAULT front-desk profile gets *some*
visible feedback, across emulated real user sessions. Two policies govern
perceived responsiveness on that surface and are both exercised here:

  * pre-LLM ack    — an immediate "on it" sent before model latency, for turns
                     that will clearly take a while (gateway.run._should_send_pre_llm_ack
                     gated by _pre_llm_ack_eligible_source).
  * public progress — periodic "still working…" notices during a long turn
                     (gateway.run._should_send_public_progress + _public_progress_phase).

The benchmark is fully deterministic: it drives those real decision functions
over a labeled session dataset and scores them with a latency model. No live
LLM and no kanban DB — it runs in CI alongside the rest of tests/.
"""
