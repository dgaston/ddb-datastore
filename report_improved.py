#!/usr/bin/env python

import re
import sys
import utils
import getpass
import argparse
import xlsxwriter

import numpy as np

from scipy import stats
from toil.job import Job
from ddb import configuration
from ddb_ngsflow import pipeline
from variantstore import Variant
from collections import defaultdict
from variantstore import SampleVariant
from coveragestore import SampleCoverage
from coveragestore import AmpliconCoverage
from cassandra.cqlengine import connection
from cassandra.auth import PlainTextAuthProvider


def get_all_amplicons(job, samples):
    job.fileStore.logToMaster(
        "Building list of all amplicons from samples set\n")
    amplicons_list = list()
    for sample in samples:
        for library in samples[sample]:
            report_panel_path = (
                "/mnt/shared-data/ddb-configs/disease_panels/{}/{}".format(
                    samples[sample][library]['panel'],
                    samples[sample][library]['report']))
            target_amplicons = utils.get_target_amplicons(report_panel_path)
            for amplicon in target_amplicons:
                if amplicon not in amplicons_list:
                    amplicons_list.append(amplicon)

    return amplicons_list


def get_coverage_data_all_amplicons(job, amplicons_list, addresses,
                                    authenticator):
    job.fileStore.logToMaster(
        "Retrieving coverage data for all libraries in database for all \
        amplicons\n")
    connection.setup(addresses, "coveragestore", auth_provider=authenticator)

    amplicon_coverage_stats = defaultdict(dict)

    for amplicon in amplicons_list:
        coverage_values = list()

        coverage_data = AmpliconCoverage.objects.timeout(None).filter(
            AmpliconCoverage.amplicon == amplicon
        )
        ordered_samples = coverage_data.order_by(
            'sample', 'run_id').limit(coverage_data.count() + 1000)
        for result in ordered_samples:
            coverage_values.append(result.mean_coverage)

        amplicon_coverage_stats[amplicon]['median'] = (
            np.median(coverage_values))
        amplicon_coverage_stats[amplicon]['std_dev'] = np.std(coverage_values)
        amplicon_coverage_stats[amplicon]['min'] = np.amin(coverage_values)
        amplicon_coverage_stats[amplicon]['max'] = np.amax(coverage_values)

    return amplicon_coverage_stats


def process_sample(job, config, sample, samples, addresses, authenticator,
                   thresholds, callers, amplicon_stats):
    job.fileStore.logToMaster("Retrieving data for sample {}\n".format(sample))
    job.fileStore.logToMaster("Retrieving coverage data from database\n")
    connection.setup(addresses, "coveragestore", auth_provider=authenticator)

    report_data = dict()
    filtered_variant_data = defaultdict(list)
    off_target_amplicon_counts = defaultdict(int)
    target_amplicon_coverage = dict()
    ordered_amplicon_coverage = list()
    reportable_amplicons = list()

    iterated = 0
    passing_variants = 0
    filtered_low_freq = 0
    filtered_low_depth = 0
    filtered_off_target = 0

    tier1_clinvar_terms = ("pathogenic", "likely-pathogenic", "drug-response")

    for library in samples[sample]:
        report_panel_path = (
            "/mnt/shared-data/ddb-configs/disease_panels/{}/{}".format(
                samples[sample][library]['panel'],
                samples[sample][library]['report']))

        job.fileStore.logToMaster(
            "{}: processing amplicons from file {}".format(
                library, report_panel_path))
        target_amplicons = utils.get_target_amplicons(report_panel_path)

        for amplicon in target_amplicons:
            coverage_data = SampleCoverage.objects.timeout(None).filter(
                SampleCoverage.sample == (
                    samples[sample][library]['sample_name']),
                SampleCoverage.amplicon == amplicon,
                SampleCoverage.run_id == samples[sample][library]['run_id'],
                SampleCoverage.library_name == (
                    samples[sample][library]['library_name']),
                SampleCoverage.program_name == "sambamba"
            )
            ordered_amplicons = (
                coverage_data.order_by('amplicon', 'run_id').limit(
                    coverage_data.count() + 1000))
            for result in ordered_amplicons:
                reportable_amplicons.append(result)
                target_amplicon_coverage[amplicon] = result
                ordered_amplicon_coverage.append(result)

        job.fileStore.logToMaster("{}: retrieving variants".format(library))
        variants = SampleVariant.objects.timeout(None).filter(
            SampleVariant.reference_genome == config['genome_version'],
            SampleVariant.sample == samples[sample][library]['sample_name'],
            SampleVariant.run_id == samples[sample][library]['run_id'],
            SampleVariant.library_name == (
                samples[sample][library]['library_name']),
            SampleVariant.max_maf_all <= thresholds['max_maf']
        ).allow_filtering()

        num_var = variants.count()
        ordered = variants.order_by('library_name', 'chr', 'pos', 'ref',
                                    'alt', 'date_annotated').limit(variants.count() + 1000)
        job.fileStore.logToMaster(
            "{}: retrieved {} variants from database\n".format(
                library, num_var))
        job.fileStore.logToMaster(
            "{}: classifying and filtering variants\n".format(library))

        for variant in ordered:
            iterated += 1
            if variant.amplicon_data['amplicon'] is 'None':
                filtered_off_target += 1
                off_target_amplicon_counts[
                    variant.amplicon_data['amplicon']] += 1
            else:
                amplicons = variant.amplicon_data['amplicon'].split(',')
                assignable = 0
                for amplicon in amplicons:
                    if amplicon in target_amplicons:
                        assignable += 1
                        break
                if assignable:
                    match_variants = Variant.objects.timeout(None).filter(
                        Variant.reference_genome == config['genome_version'],
                        Variant.chr == variant.chr,
                        Variant.pos == variant.pos,
                        Variant.ref == variant.ref,
                        Variant.alt == variant.alt
                    ).allow_filtering()

                    num_matches = match_variants.count()
                    ordered_var = match_variants.order_by(
                        'pos', 'ref', 'alt', 'sample', 'library_name',
                        'run_id').limit(num_matches + 1000)
                    vafs = list()
                    run_vafs = list()
                    run_match_samples = list()
                    num_times_callers = defaultdict(int)
                    num_times_in_run = 0

                    for var in ordered_var:
                        vaf = var.max_som_aaf
                        vafs.append(vaf)
                        if var.run_id == variant.run_id:
                            num_times_in_run += 1
                            run_vafs.append(vaf)
                            run_match_samples.append(var.library_name)
                        for caller in var.callers:
                            num_times_callers[caller] += 1

                    variant.vaf_median = np.median(vafs)
                    variant.vaf_std_dev = np.std(vafs)
                    variant.run_median = np.median(run_vafs)
                    variant.vaf_perc_rank = stats.percentileofscore(
                        vafs, variant.max_som_aaf, kind="mean")
                    variant.num_times_called = num_matches
                    variant.num_times_run = num_times_in_run
                    variant.matching_samples = run_match_samples

                    caller_counts_elements = list()
                    for caller in num_times_callers:
                        caller_counts_elements.append("{}: {}".format(
                            caller, num_times_callers[caller]))
                    variant.num_times_callers = ",".join(
                        caller_counts_elements)

                    # Putting in to Tier1 based on COSMIC
                    if variant.cosmic_ids:
                        if variant.max_som_aaf < thresholds['min_saf']:
                            filtered_variant_data[
                                'cosmic_clinvar_fail'].append(variant)
                            filtered_low_freq += 1
                        elif variant.max_depth < thresholds['depth']:
                            filtered_variant_data[
                                'cosmic_clinvar_fail'].append(variant)
                            filtered_low_depth += 1
                        else:
                            filtered_variant_data[
                                'cosmic_clinvar'].append(variant)
                            passing_variants += 1
                        continue

                    # Putting in to Tier1 based on ClinVar
                    if any(
                        i in tier1_clinvar_terms for i in variant.clinvar_data[
                            'significance']):
                        if variant.max_som_aaf < thresholds['min_saf']:
                            filtered_variant_data[
                                'cosmic_clinvar_fail'].append(variant)
                            filtered_low_freq += 1
                        elif variant.max_depth < thresholds['depth']:
                            filtered_variant_data[
                                'cosmic_clinvar_fail'].append(variant)
                            filtered_low_depth += 1
                        else:
                            filtered_variant_data[
                                'cosmic_clinvar'].append(variant)
                            passing_variants += 1
                        continue

                    if variant.severity == 'HIGH':
                        if variant.max_som_aaf < thresholds['min_saf']:
                            filtered_variant_data[
                                'high_impact_fail'].append(variant)
                            filtered_low_freq += 1
                        elif variant.max_depth < thresholds['depth']:
                            filtered_variant_data[
                                'high_impact_fail'].append(variant)
                            filtered_low_depth += 1
                        else:
                            filtered_variant_data[
                                'high_impact'].append(variant)
                            passing_variants += 1
                        continue
                    elif variant.severity == 'MED':
                        if variant.max_som_aaf < thresholds['min_saf']:
                            filtered_variant_data[
                                'med_impact_fail'].append(variant)
                            filtered_low_freq += 1
                        elif variant.max_depth < thresholds['depth']:
                            filtered_variant_data[
                                'med_impact_fail'].append(variant)
                            filtered_low_depth += 1
                        else:
                            filtered_variant_data[
                                'med_impact'].append(variant)
                            passing_variants += 1
                        continue
                    else:
                        if variant.max_som_aaf < thresholds['min_saf']:
                            filtered_variant_data[
                                'low_impact_fail'].append(variant)
                            filtered_low_freq += 1
                        elif variant.max_depth < thresholds['depth']:
                            filtered_variant_data[
                                'low_impact_fail'].append(variant)
                            filtered_low_depth += 1
                        else:
                            filtered_variant_data[
                                'low_impact'].append(variant)
                            passing_variants += 1
                        continue
                else:
                    filtered_off_target += 1
                    off_target_amplicon_counts[variant.amplicon_data[
                        'amplicon']] += 1

        job.fileStore.logToMaster(
            "{}: iterated through {} variants\n".format(library, iterated))
        job.fileStore.logToMaster(
            "{}: passing {} tier 1 and 2 variants\n".format(
                library, len(filtered_variant_data['tier1_pass_variants'])))
        job.fileStore.logToMaster(
            "{}: passing {} tier3 variants\n".format(
                library, len(filtered_variant_data['tier3_pass_variants'])))
        job.fileStore.logToMaster(
            "{}: passing {} tier 4 variants\n".format(
                library, len(filtered_variant_data['tier4_pass_variants'])))

    report_data['variants'] = filtered_variant_data
    report_data['coverage'] = target_amplicon_coverage

    report_name = "{}.xlsx".format(sample)

    wb = xlsxwriter.Workbook(report_name)

    error_style = wb.add_format({'bg_color': '#FF0000'})
    warning_style = wb.add_format({'bg_color': '#FF9900'})
    pass_style = wb.add_format({'bg_color': '#00FF00'})
    interest_style = wb.add_format({'bg_color': '#d3d3d3'})
    default_style = wb.add_format({'bg_color': '#FFFFFF'})

    coverage_sheet = wb.add_worksheet("Coverage")
    report_sheet = wb.add_worksheet("Notable Variants")
    cosmic_clinvar_pass_sheet  = wb.add_worksheet("COSMIC and ClinVar Variants")
    high_impact_pass_sheet = wb.add_worksheet("Other High Impact Variants")
    med_impact_pass_sheet = wb.add_worksheet("Other Missense Variants")
    low_impact_pass_sheet = wb.add_worksheet("Other Low Impact Variants")

    cosmic_clinvar_fail_sheet  = wb.add_worksheet("FAIL - COSMIC and ClinVar")
    high_impact_fail_sheet = wb.add_worksheet("FAIL - High Impact")
    med_impact_fail_sheet = wb.add_worksheet("FAIL - Missense")
    low_impact_fail_sheet = wb.add_worksheet("Fail - Low Impact")

    tier_sheets = (cosmic_clinvar_pass_sheet, high_impact_pass_sheet, med_impact_pass_sheet,
                   low_impact_pass_sheet, cosmic_clinvar_fail_sheet, high_impact_fail_sheet,
                   med_impact_fail_sheet, low_impact_fail_sheet)
    tier_key = ("cosmic_clinvar", "high_impact",
                "med_impact", "low_impact",
                "cosmic_clinvar_fail", "high_impact_fail",
                "med_impact_fail", "low_impact_fail")

    libraries = list()
    report_templates = list()
    run_id = ""
    for library in samples[sample]:
        libraries.append(samples[sample][library]['library_name'])
        report_templates.append(samples[sample][library]['report'])
        run_id = samples[sample][library]['run_id']
    lib_string = " | ".join(libraries)
    reports_string = " | ".join(report_templates)

    coverage_sheet.write(0, 0, "Sample")
    coverage_sheet.write(0, 1, "{}".format(sample))

    coverage_sheet.write(1, 0, "Libraries")
    coverage_sheet.write(1, 1, "{}".format(lib_string))

    coverage_sheet.write(2, 0, "Run ID")
    coverage_sheet.write(2, 1, "{}".format(run_id))

    coverage_sheet.write(3, 0, "Reporting Templates")
    coverage_sheet.write(3, 1, "{}".format(reports_string))

    coverage_sheet.write(4, 0, "Minimum Reportable Somatic Allele Frequency")
    coverage_sheet.write(4, 1, "{}".format(thresholds['min_saf']))

    coverage_sheet.write(5, 0, "Minimum Amplicon Depth")
    coverage_sheet.write(5, 1, "{}".format(thresholds['depth']))

    coverage_sheet.write(6, 0, "Maximum Population Allele Frequency")
    coverage_sheet.write(6, 1, "{}".format(thresholds['max_maf']))

    coverage_sheet.write(7, 0, "Sample")
    coverage_sheet.write(7, 1, "Library")
    coverage_sheet.write(7, 2, "Amplicon")
    coverage_sheet.write(7, 3, "Num Reads")
    coverage_sheet.write(7, 4, "Coverage")
    coverage_sheet.write(7, 5, "Database Median")
    coverage_sheet.write(7, 6, "Database Std Dev")

    row_num = 8
    for amplicon in reportable_amplicons:
        if amplicon.mean_coverage < 200:
            style = error_style
        elif amplicon.mean_coverage < 500:
            style = warning_style
        else:
            style = pass_style

        coverage_sheet.write(row_num, 0, "{}".format(amplicon.sample), style)
        coverage_sheet.write(row_num, 1, "{}".format(amplicon.library_name),
                             style)
        coverage_sheet.write(row_num, 2, "{}".format(amplicon.amplicon), style)
        coverage_sheet.write(row_num, 3, "{}".format(amplicon.num_reads),
                             style)
        coverage_sheet.write(row_num, 4, "{}".format(amplicon.mean_coverage),
                             style)
        coverage_sheet.write(row_num, 5,
                             "{}".format(
                                 amplicon_stats[amplicon.amplicon]['median']),
                             style)
        coverage_sheet.write(row_num, 6,
                             "{}".format(
                                 amplicon_stats[amplicon.amplicon]['std_dev']),
                             style)

        row_num += 1

    ###########################################################################

    sheet_num = 0
    for sheet in tier_sheets:
        sheet.write(0, 0, "Sample")
        sheet.write(0, 1, "Library")
        sheet.write(0, 2, "Gene")
        sheet.write(0, 3, "Amplicon")
        sheet.write(0, 4, "Ref")
        sheet.write(0, 5, "Alt")
        sheet.write(0, 6, "Codon")
        sheet.write(0, 7, "AA")
        sheet.write(0, 8, "Max Caller Somatic VAF")
        sheet.write(0, 9, "Num Times in Database")
        sheet.write(0, 10, "Num Times in Run")
        sheet.write(0, 11, "Median VAF in DB")
        sheet.write(0, 12, "Median VAF in Run")
        sheet.write(0, 13, "StdDev VAF")
        sheet.write(0, 14, "VAF Percentile Rank")
        sheet.write(0, 15, "Callers")
        sheet.write(0, 16, "Caller Counts")
        sheet.write(0, 17, "COSMIC IDs")
        sheet.write(0, 18, "Num COSMIC Samples")
        sheet.write(0, 19, "COSMIC AA")
        sheet.write(0, 20, "Clinvar Significance")
        sheet.write(0, 21, "Clinvar HGVS")
        sheet.write(0, 22, "Clinvar Disease")
        sheet.write(0, 23, "Coverage")
        sheet.write(0, 24, "Num Reads")
        sheet.write(0, 25, "Impact")
        sheet.write(0, 26, "Severity")
        sheet.write(0, 27, "Maximum Population AF")
        sheet.write(0, 28, "Min Caller Depth")
        sheet.write(0, 29, "Max Caller Depth")
        sheet.write(0, 30, "Chrom")
        sheet.write(0, 31, "Start")
        sheet.write(0, 32, "End")
        sheet.write(0, 33, "rsIDs")
        sheet.write(0, 34, "Matching Samples in Run")

        col = 35
        if 'mutect' in callers:
            sheet.write(0, col, "MuTect_AF")
            col += 1

        if 'vardict' in callers:
            sheet.write(0, col, "VarDict_AF")
            col += 1

        if 'freebayes' in callers:
            sheet.write(0, col, "FreeBayes_AF")
            col += 1

        if 'scalpel' in callers:
            sheet.write(0, col, "Scalpel_AF")
            col += 1

        if 'platypus' in callers:
            sheet.write(0, col, "Platypus_AF")
            col += 1

        if 'pindel' in callers:
            sheet.write(0, col, "Pindel_AF")
            col += 1

        row = 1

        cosmic_nums = re.findall(r'\b\d+\b', variant.cosmic_data['num_samples'])

        for variant in report_data['variants'][tier_key[sheet_num]]:
            num_cosmic = 0
            for num in cosmic_nums:
                if int(num) > num_cosmic:
                    num_cosmic = int(num)

            num_callers = len(variant.callers)
            freebayes_pindel_only = 0
            if num_callers == 1:
                if 'freebayes' in variant.callers:
                    freebayes_pindel_only = 1
                elif 'pindel' in variant.callers:
                    freebayes_pindel_only = 1
                else:
                    freebayes_pindel_only = 0

            if freebayes_pindel_only:
                style = error_style
            elif "pathogenic" in variant.clinvar_data['significance']:
                style = pass_style
            elif "drug-response" in variant.clinvar_data['significance']:
                style = pass_style
            elif "likely-pathogenic" in variant.clinvar_data['significance']:
                style = pass_style
            elif variant.max_som_aaf > 0.05:
                style = interest_style
            elif num_cosmic >= 5:
                style = interest_style
            else:
                style = default_style

            amplicons = variant.amplicon_data['amplicon'].split(',')

            coverage_values = list()
            reads_values = list()
            for amplicon in amplicons:
                coverage_values.append(
                    str(report_data['coverage'][amplicon]['mean_coverage']))
                reads_values.append(
                    str(report_data['coverage'][amplicon]['num_reads']))

            coverage_string = ",".join(coverage_values)
            reads_string = ",".join(reads_values)

            if len(variant.ref) < 200:
                ref = variant.ref
            else:
                ref = "Length > 200bp"

            if len(variant.alt) < 200:
                alt = variant.alt
            else:
                alt = "Length > 200bp"

            if len(variant.codon_change) < 200:
                codon_change = variant.codon_change
            else:
                codon_change = "Length > 200aa"

            if len(variant.aa_change) < 200:
                aa_change = variant.aa_change
            else:
                aa_change = "Length > 200aa"

            sheet.write(row, 0, "{}".format(variant.sample), style)
            sheet.write(row, 1, "{}".format(variant.library_name), style)
            sheet.write(row, 2, "{}".format(variant.gene), style)
            sheet.write(row, 3, "{}".format(variant.amplicon_data['amplicon']),
                        style)
            sheet.write(row, 4, "{}".format(ref), style)
            sheet.write(row, 5, "{}".format(alt), style)
            sheet.write(row, 6, "{}".format(codon_change), style)
            sheet.write(row, 7, "{}".format(aa_change), style)
            sheet.write(row, 8, "{}".format(variant.max_som_aaf), style)
            sheet.write(row, 9, "{}".format(variant.num_times_called), style)
            sheet.write(row, 10, "{}".format(variant.num_times_run), style)
            sheet.write(row, 11, "{}".format(variant.vaf_median), style)
            sheet.write(row, 12, "{}".format(variant.run_median), style)
            sheet.write(row, 13, "{}".format(variant.vaf_std_dev), style)
            sheet.write(row, 14, "{}".format(variant.vaf_perc_rank), style)
            sheet.write(row, 15, "{}".format(",".join(variant.callers)
                                             or None), style)
            sheet.write(row, 16, "{}".format(variant.num_times_callers), style)
            sheet.write(row, 17, "{}".format(",".join(variant.cosmic_ids)
                                             or None), style)
            sheet.write(row, 18,
                        "{}".format(variant.cosmic_data['num_samples']), style)
            sheet.write(row, 19, "{}".format(variant.cosmic_data['aa']), style)
            sheet.write(row, 20,
                        "{}".format(variant.clinvar_data['significance']),
                        style)
            sheet.write(row, 21,
                        "{}".format(variant.clinvar_data['hgvs']), style)
            sheet.write(row, 22,
                        "{}".format(variant.clinvar_data['disease']), style)
            sheet.write(row, 23, "{}".format(coverage_string), style)
            sheet.write(row, 24, "{}".format(reads_string), style)
            sheet.write(row, 25, "{}".format(variant.impact), style)
            sheet.write(row, 26, "{}".format(variant.severity), style)
            sheet.write(row, 27, "{}".format(variant.max_maf_all), style)
            sheet.write(row, 28, "{}".format(variant.min_depth), style)
            sheet.write(row, 29, "{}".format(variant.max_depth), style)
            sheet.write(row, 30, "{}".format(variant.chr), style)
            sheet.write(row, 31, "{}".format(variant.pos), style)
            sheet.write(row, 32, "{}".format(variant.end), style)
            sheet.write(row, 33, "{}".format(",".join(variant.rs_ids)), style)
            sheet.write(row, 34,
                        "{}".format(",".join(variant.matching_samples)), style)

            col = 35
            if 'mutect' in callers:
                sheet.write(row, col, "{}".format(variant.mutect.get('AAF')
                                                  or None), style)
                col += 1

            if 'vardict' in callers:
                sheet.write(row, col, "{}".format(variant.vardict.get('AAF')
                                                  or None), style)
                col += 1

            if 'freebayes' in callers:
                sheet.write(row, col, "{}".format(variant.freebayes.get('AAF')
                                                  or None), style)
                col += 1

            if 'scalpel' in callers:
                sheet.write(row, col, "{}".format(variant.scalpel.get('AAF')
                                                  or None), style)
                col += 1

            if 'platypus' in callers:
                sheet.write(row, col, "{}".format(variant.platypus.get('AAF')
                                                  or None), style)
                col += 1

            if 'pindel' in callers:
                sheet.write(row, col, "{}".format(variant.pindel.get('AAF')
                                                  or None), style)
                col += 1

            row += 1
        sheet_num += 1
    wb.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--samples_file',
                        help="Input configuration file for samples")
    parser.add_argument('-c', '--configuration',
                        help="Configuration file for various settings")
    parser.add_argument('-r', '--report',
                        help="Root name for reports (per sample)",
                        default='report')
    parser.add_argument('-a', '--address',
                        help="IP Address for Cassandra connection",
                        default='127.0.0.1')
    parser.add_argument('-u', '--username',
                        help='Cassandra username for login',
                        default=None)
    parser.add_argument('-d', '--min_depth',
                        help='Minimum depth threshold for variant reporting',
                        default=200.0)
    parser.add_argument('-g', '--good_depth',
                        help='Floor for good depth of coverage',
                        default=500.0)
    parser.add_argument('-t', '--min_somatic_var_freq',
                        help='Minimum reportable somatic variant frequency',
                        default=0.01)
    parser.add_argument('-p', '--max_pop_freq',
                        help='Maximum allowed population allele frequency',
                        default=0.005)
    Job.Runner.addToilOptions(parser)
    args = parser.parse_args()
    args.logLevel = "INFO"

    sys.stdout.write("Parsing configuration data\n")
    config = configuration.configure_runtime(args.configuration)

    sys.stdout.write("Parsing sample data\n")
    libraries = configuration.configure_samples(args.samples_file, config)

    samples = configuration.merge_library_configs_samples(libraries)

    if args.username:
        password = getpass.getpass()
        auth_provider = PlainTextAuthProvider(username=args.username,
                                              password=password)
    else:
        auth_provider = None

    thresholds = {'min_saf': args.min_somatic_var_freq,
                  'max_maf': args.max_pop_freq,
                  'depth': args.min_depth}

    callers = ("mutect", "platypus", "vardict", "scalpel", "freebayes",
               "pindel")

    sys.stdout.write("Processing samples\n")
    root_job = Job.wrapJobFn(pipeline.spawn_batch_jobs, cores=1)
    amplicons_list_job = Job.wrapJobFn(get_all_amplicons, samples)
    all_amplicon_coverage_job = Job.wrapJobFn(get_coverage_data_all_amplicons,
                                              amplicons_list_job.rv(),
                                              [args.address], auth_provider)
    spawn_samples_job = Job.wrapJobFn(pipeline.spawn_variant_jobs)

    root_job.addChild(amplicons_list_job)
    amplicons_list_job.addChild(all_amplicon_coverage_job)
    all_amplicon_coverage_job.addChild(spawn_samples_job)

    amplicon_stats = all_amplicon_coverage_job.rv()

    for sample in samples:
        sample_job = Job.wrapJobFn(process_sample, config, sample, samples,
                                   [args.address], auth_provider,
                                   thresholds, callers, amplicon_stats,
                                   cores=1)

        spawn_samples_job.addChild(sample_job)

    # Start workflow execution
    Job.Runner.startToil(root_job, args)
