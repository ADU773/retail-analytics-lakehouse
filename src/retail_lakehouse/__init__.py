"""retail_lakehouse: a laptop-scale analytics lakehouse for Aurora Goods Co.

The package covers the full analytics-engineering lifecycle:

* :mod:`retail_lakehouse.generator` - reproducible, parallel synthetic data with injected
  data-quality problems, landed as CSV / JSON Lines batches.
* :mod:`retail_lakehouse.bronze` - idempotent ingestion into Hive-partitioned Parquet.
* :mod:`retail_lakehouse.transform` - a mini-dbt DAG runner for silver/gold SQL models.
* :mod:`retail_lakehouse.quality` - declarative YAML data-quality tests.
* :mod:`retail_lakehouse.reporting` - static Plotly report, PNG charts and insights.
"""

__version__ = "0.1.0"
COMPANY_NAME = "Aurora Goods Co."

__all__ = ["COMPANY_NAME", "__version__"]
