import math
import statistics
from collections import Counter


def describe(records):
    response = [row for row in records if row['responded'] and row['adoption'] is not None]
    weight = math.fsum(row['base_weight'] for row in response)
    return {'respondents': len(response), 'response_weight': weight, 'adoption_weighted': math.fsum(row['adoption'] * row['base_weight'] for row in response) / weight if weight else None}
