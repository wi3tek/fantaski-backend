import json

def get_distinct_values(json_file, field_name):
    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    distinct_values = set()

    for event in data:
        for comp in event.get("sessions", []):
            value = comp.get(field_name)
            if value:
                # Jeśli value jest stringiem, robimy strip. Jeśli nie, zostawiamy jak jest.
                if isinstance(value, str):
                    distinct_values.add(value.strip())
                else:
                    distinct_values.add(value)

    return sorted(list(distinct_values))

file_path = "wyniki_sezonu_2025_M.json"
fields_to_check = ["title", "category", "gender"]
results = {}

for field in fields_to_check:
    results[field] = get_distinct_values(file_path, field)

print(json.dumps(results, indent=4, ensure_ascii=False))