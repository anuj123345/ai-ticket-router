{#
  on-run-end hook: write one row per dbt invocation to public.pipeline_runs.
  Runs even when models or tests fail, so failures are recorded too.
  Not logged: compile, docs, debug, freshness, or runs with nothing selected.
#}
{% macro log_run_results(results) %}
  {% if execute and flags.WHICH in ['build', 'run', 'test', 'snapshot', 'seed'] and results | length > 0 %}
    {% set ns = namespace(models=0, tests=0, snapshots=0, failed=0, warned=0, skipped=0, rows=0) %}
    {% set nodes = [] %}
    {% set problems = [] %}
    {% for r in results %}
      {% set status = r.status | string | lower %}
      {% set kind = r.node.resource_type | string | lower %}
      {% set rows = (r.adapter_response or {}).get('rows_affected') %}
      {% if kind == 'model' %}{% set ns.models = ns.models + 1 %}{% endif %}
      {% if kind == 'test' %}{% set ns.tests = ns.tests + 1 %}{% endif %}
      {% if kind == 'snapshot' %}{% set ns.snapshots = ns.snapshots + 1 %}{% endif %}
      {% if status in ['error', 'fail', 'runtimeerror'] %}
        {% set ns.failed = ns.failed + 1 %}
        {% do problems.append(r.node.name ~ ': ' ~ (r.message or status)) %}
      {% elif status == 'warn' %}
        {% set ns.warned = ns.warned + 1 %}
        {% do problems.append(r.node.name ~ ' (warning): ' ~ (r.message or status)) %}
      {% elif status == 'skipped' %}
        {% set ns.skipped = ns.skipped + 1 %}
      {% endif %}
      {% if kind in ['model', 'snapshot'] and rows is not none and rows >= 0 %}
        {% set ns.rows = ns.rows + rows %}
      {% endif %}
      {# Keep the log small: every model and snapshot, but only tests that didn't pass. #}
      {% if kind != 'test' or status != 'pass' %}
      {% do nodes.append({
          'name': r.node.name,
          'type': kind,
          'status': status,
          'rows': rows,
          'seconds': (r.execution_time or 0) | round(2),
          'failures': r.failures
      }) %}
      {% endif %}
    {% endfor %}

    {% set details = {
        'command': flags.WHICH,
        'target': target.name,
        'models': ns.models,
        'tests': ns.tests,
        'snapshots': ns.snapshots,
        'failed': ns.failed,
        'warned': ns.warned,
        'skipped': ns.skipped,
        'nodes': nodes
    } %}
    {% set run_status = 'failed' if ns.failed > 0 else 'success' %}
    {% set error_text = (problems | join('\n'))[:4000] %}

    insert into public.pipeline_runs
      (pipeline, run_key, status, started_at, finished_at, rows_out, details, error)
    values (
      'dbt',
      '{{ invocation_id }}',
      '{{ run_status }}',
      '{{ run_started_at.isoformat() }}'::timestamptz,
      now(),
      {{ ns.rows }},
      '{{ details | tojson | replace("'", "''") }}'::jsonb,
      {% if error_text %}'{{ error_text | replace("'", "''") }}'{% else %}null{% endif %}
    )
  {% endif %}
{% endmacro %}
