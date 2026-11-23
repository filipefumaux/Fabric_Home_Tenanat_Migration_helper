TARGET_WORKSPACE_ID = ""   # empty = the workspace this notebook is in
PREFIX = "bkp_test"        # every test item name starts with this
CREATE_TYPES = ["Lakehouse", "Warehouse", "Eventhouse", "Notebook", "DataPipeline", "Dataflow",
                "DataAgent", "GraphModel", "Ontology"]   # SQLEndpoint and KQLDatabase come with their parents
LARGE_DATA_MIB = 0         # 0 = original small fixtures; >0 = extra owned stress table bytes per storage family
MEASURE_DATA_SIZE = False  # True = also measure persisted sizes in small mode; never accept unpublished/zero sizes
ISOLATED_STRESS_RUNTIME = False  # True = use a hash-pinned Delta wheel in this GB seed job only; needs PyArrow 16+
