from cassandra.cqlengine import columns
from cassandra.cqlengine.models import Model


class SampleCoverage(Model):
    __keyspace__ = 'coveragestore'
    sample = columns.Text(primary_key=True, partition_key=True)

    run_id = columns.Text(primary_key=True)
    library_name = columns.Text(primary_key=True)
    program_name = columns.Text(primary_key=True)

    num_libraries_in_run = columns.Integer()
    sequencer_id = columns.Text()

    amplicon_depth = columns.Map(columns.Text, columns.Integer)
    thresholds = columns.List(columns.Integer)
    perc_bp_cov_at_thresholds = columns.Map(columns.Integer, columns.Float)


class AmpliconCoverage(Model):
    __keyspace__ = 'coveragestore'
    amplicon = columns.Text(primary_key=True, partition_key=True)

    sample = columns.Text(primary_key=True)
    run_id = columns.Text(primary_key=True)
    library_name = columns.Text(primary_key=True)
    program_name = columns.Text(primary_key=True)

    num_libraries_in_run = columns.Integer()
    sequencer_id = columns.Text()

    depth = columns.Integer()
    thresholds = columns.List(columns.Integer)
    perc_bp_cov_at_thresholds = columns.Map(columns.Integer, columns.Float)
