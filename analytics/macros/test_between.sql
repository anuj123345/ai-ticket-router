{#
  Generic test: every non-null value lies between min_value and max_value.
  Usage in a .yml file:
      data_tests:
        - between: {min_value: 0, max_value: 1}
#}
{% test between(model, column_name, min_value, max_value) %}
select {{ column_name }}
  from {{ model }}
 where {{ column_name }} is not null
   and ({{ column_name }} < {{ min_value }} or {{ column_name }} > {{ max_value }})
{% endtest %}
