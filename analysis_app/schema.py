FIELDS = {'observation_id': 'str', 'firm': 'str', 'region': 'str', 'size_class': 'str', 'industry': 'str', 'adoption': 'float', 'responded': 'int', 'base_weight': 'float', 'wave': 'int'}
REQUIRED = ['observation_id', 'firm', 'region', 'adoption']
ENTITY = 'firm'
GROUP = 'region'
MEASURE = 'adoption'
PRODUCT = 'survey-calibration'
UNIT = '二元值'
