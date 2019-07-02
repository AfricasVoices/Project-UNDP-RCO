import argparse
import json
import os

from core_data_modules.logging import Logger
from core_data_modules.traced_data.io import TracedDataJsonIO
from core_data_modules.util import IOUtils
from storage.google_cloud import google_cloud_utils
from storage.google_drive import drive_client_wrapper

from src import CombineRawDatasets, TranslateRapidProKeys, AutoCodeShowMessages, AutoCodeSurveys, ProductionFile
from src.lib import PipelineConfiguration, MessageFilters
from src.lib.pipeline_configuration import CodeSchemes

Logger.set_project_name("UNDP-RCO")
log = Logger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Runs the post-fetch phase of the ReDSS pipeline",
                                     # Support \n and long lines
                                     formatter_class=argparse.RawTextHelpFormatter)

    parser.add_argument("user", help="User launching this program")
    parser.add_argument("google_cloud_credentials_file_path", metavar="google-cloud-credentials-file-path",
                        help="Path to a Google Cloud service account credentials file to use to access the "
                             "credentials bucket")
    parser.add_argument("pipeline_configuration_file_path", metavar="pipeline-configuration-file",
                        help="Path to the pipeline configuration json file")

    parser.add_argument("raw_data_dir", metavar="raw-data-dir",
                        help="Path to a directory containing the raw data files exported by fetch_raw_data.py")
    parser.add_argument("prev_coded_dir_path", metavar="prev-coded-dir-path",
                        help="Directory containing Coda files generated by a previous run of this pipeline. "
                             "New data will be appended to these files.")

    parser.add_argument("messages_json_output_path", metavar="messages-json-output-path",
                        help="Path to a JSON file to write the TracedData associated with the messages analysis file")
    parser.add_argument("individuals_json_output_path", metavar="individuals-json-output-path",
                        help="Path to a JSON file to write the TracedData associated with the individuals analysis file")
    parser.add_argument("icr_output_dir", metavar="icr-output-dir",
                        help="Directory to write CSV files to, each containing 200 messages and message ids for use " 
                             "in inter-code reliability evaluation"),
    parser.add_argument("coded_dir_path", metavar="coded-dir-path",
                        help="Directory to write coded Coda files to")
    parser.add_argument("csv_by_message_output_path", metavar="csv-by-message-output-path",
                        help="Analysis dataset where messages are the unit for analysis (i.e. one message per row)")
    parser.add_argument("csv_by_individual_output_path", metavar="csv-by-individual-output-path",
                        help="Analysis dataset where respondents are the unit for analysis (i.e. one respondent "
                             "per row, with all their messages joined into a single cell)")
    parser.add_argument("production_csv_output_path", metavar="production-csv-output-path",
                        help="Path to a CSV file to write raw message and demographic responses to, for use in "
                             "radio show production"),

    args = parser.parse_args()

    csv_by_message_drive_path = None
    csv_by_individual_drive_path = None
    production_csv_drive_path = None

    user = args.user
    pipeline_configuration_file_path = args.pipeline_configuration_file_path
    google_cloud_credentials_file_path = args.google_cloud_credentials_file_path

    raw_data_dir = args.raw_data_dir
    prev_coded_dir_path = args.prev_coded_dir_path

    messages_json_output_path = args.messages_json_output_path
    individuals_json_output_path = args.individuals_json_output_path
    icr_output_dir = args.icr_output_dir
    coded_dir_path = args.coded_dir_path
    csv_by_message_output_path = args.csv_by_message_output_path
    csv_by_individual_output_path = args.csv_by_individual_output_path
    production_csv_output_path = args.production_csv_output_path

    # Load the pipeline configuration file
    log.info("Loading Pipeline Configuration File...")
    with open(pipeline_configuration_file_path) as f:
        pipeline_configuration = PipelineConfiguration.from_configuration_file(f)

    if pipeline_configuration.drive_upload is not None:
        log.info(f"Downloading Google Drive service account credentials...")
        credentials_info = json.loads(google_cloud_utils.download_blob_to_string(
            google_cloud_credentials_file_path, pipeline_configuration.drive_upload.drive_credentials_file_url))
        drive_client_wrapper.init_client_from_info(credentials_info)

    # Load messages
    messages_datasets = []
    for i, activation_flow_name in enumerate(pipeline_configuration.activation_flow_names):
        raw_activation_path = f"{raw_data_dir}/{activation_flow_name}.json"
        log.info(f"Loading {raw_activation_path}...")
        with open(raw_activation_path, "r") as f:
            messages = TracedDataJsonIO.import_json_to_traced_data_iterable(f)
        log.info(f"Loaded {len(messages)} messages")
        messages_datasets.append(messages)

    log.info("Loading surveys datasets:")
    surveys_datasets = []
    for i, survey_flow_name in enumerate(pipeline_configuration.survey_flow_names):
        raw_survey_path = f"{raw_data_dir}/{survey_flow_name}.json"
        log.info(f"Loading {raw_survey_path}...")
        with open(raw_survey_path, "r") as f:
            contacts = TracedDataJsonIO.import_json_to_traced_data_iterable(f)
        log.info(f"Loaded {len(contacts)} contacts")
        surveys_datasets.append(contacts)

    # Add survey data to the messages
    log.info("Combining Datasets...")
    coalesced_surveys_datasets = []
    for dataset in surveys_datasets:
        coalesced_surveys_datasets.append(CombineRawDatasets.coalesce_traced_runs_by_key(user, dataset, "avf_phone_id"))
    data = CombineRawDatasets.combine_raw_datasets(user, messages_datasets, coalesced_surveys_datasets)

    if pipeline_configuration.filter_operator is not None:
        data = MessageFilters.filter_operator(
            data, "operator_coded",
            CodeSchemes.SOMALIA_OPERATOR.get_code_with_match_value(pipeline_configuration.filter_operator)
        )

    log.info("Translating Rapid Pro Keys...")
    data = TranslateRapidProKeys.translate_rapid_pro_keys(user, data, pipeline_configuration, prev_coded_dir_path)

    log.info("Auto Coding Messages...")
    data = AutoCodeShowMessages.auto_code_show_messages(user, data, pipeline_configuration, icr_output_dir, coded_dir_path)

    log.info("Exporting production CSV...")
    data = ProductionFile.generate(data, production_csv_output_path)

    log.info("Auto Coding Surveys...")
    data = AutoCodeSurveys.auto_code_surveys(user, data, coded_dir_path)
    #
    # log.info("Applying Manual Codes from Coda...")
    # data = ApplyManualCodes.apply_manual_codes(user, data, prev_coded_dir_path)
    #
    # log.info("Generating Analysis CSVs...")
    # messages_data, individuals_data = AnalysisFile.generate(user, data, csv_by_message_output_path,
    #                                                         csv_by_individual_output_path)

    # TODO: Delete when the generate analysis CSVs stage is activated
    messages_data = data

    log.info("Writing messages TracedData to file...")
    IOUtils.ensure_dirs_exist_for_file(messages_json_output_path)
    with open(messages_json_output_path, "w") as f:
        TracedDataJsonIO.export_traced_data_iterable_to_json(messages_data, f, pretty_print=True)

    # log.info("Writing individuals TracedData to file...")
    # IOUtils.ensure_dirs_exist_for_file(individuals_json_output_path)
    # with open(individuals_json_output_path, "w") as f:
    #     TracedDataJsonIO.export_traced_data_iterable_to_json(individuals_data, f, pretty_print=True)

    # Upload to Google Drive, if requested.
    # Note: This should happen as late as possible in order to reduce the risk of the remainder of the pipeline failing
    # after a Drive upload has occurred. Failures could result in inconsistent outputs or outputs with no
    # traced data log.
    if pipeline_configuration.drive_upload is not None:
        log.info("Uploading CSVs to Google Drive...")

        production_csv_drive_dir = os.path.dirname(pipeline_configuration.drive_upload.production_upload_path)
        production_csv_drive_file_name = os.path.basename(pipeline_configuration.drive_upload.production_upload_path)
        drive_client_wrapper.update_or_create(production_csv_output_path, production_csv_drive_dir,
                                              target_file_name=production_csv_drive_file_name,
                                              target_folder_is_shared_with_me=True)

        # messages_csv_drive_dir = os.path.dirname(pipeline_configuration.drive_upload.messages_upload_path)
        # messages_csv_drive_file_name = os.path.basename(pipeline_configuration.drive_upload.messages_upload_path)
        # drive_client_wrapper.update_or_create(csv_by_message_output_path, messages_csv_drive_dir,
        #                                       target_file_name=messages_csv_drive_file_name,
        #                                       target_folder_is_shared_with_me=True)
        #
        # individuals_csv_drive_dir = os.path.dirname(pipeline_configuration.drive_upload.individuals_upload_path)
        # individuals_csv_drive_file_name = os.path.basename(pipeline_configuration.drive_upload.individuals_upload_path)
        # drive_client_wrapper.update_or_create(csv_by_individual_output_path, individuals_csv_drive_dir,
        #                                       target_file_name=individuals_csv_drive_file_name,
        #                                       target_folder_is_shared_with_me=True)

        traced_data_drive_dir = os.path.dirname(pipeline_configuration.drive_upload.messages_traced_data_upload_path)
        traced_data_drive_file_name = os.path.basename(
            pipeline_configuration.drive_upload.messages_traced_data_upload_path)
        drive_client_wrapper.update_or_create(messages_json_output_path, traced_data_drive_dir,
                                              target_file_name=traced_data_drive_file_name,
                                              target_folder_is_shared_with_me=True)

        # traced_data_drive_dir = os.path.dirname(pipeline_configuration.drive_upload.individuals_traced_data_upload_path)
        # traced_data_drive_file_name = os.path.basename(
        #     pipeline_configuration.drive_upload.individuals_traced_data_upload_path)
        # drive_client_wrapper.update_or_create(individuals_json_output_path, traced_data_drive_dir,
        #                                       target_file_name=traced_data_drive_file_name,
        #                                       target_folder_is_shared_with_me=True)
    else:
        log.info("Skipping uploading to Google Drive (because the pipeline configuration json does not contain the key "
                 "'DriveUploadPaths')")

    log.info("Python script complete")