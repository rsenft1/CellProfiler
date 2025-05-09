import io
import json
import logging
import logging.config
import argparse
from argparse import RawTextHelpFormatter
import os
import os.path
import sys
import tempfile
import urllib.parse

import h5py
import matplotlib
import numpy

from cellprofiler import __version__ as cellprofiler_version

from cellprofiler_core.constants.measurement import EXPERIMENT
from cellprofiler_core.constants.measurement import GROUP_INDEX
from cellprofiler_core.constants.measurement import GROUP_NUMBER
from cellprofiler_core.constants.measurement import IMAGE
from cellprofiler_core.constants.pipeline import M_PIPELINE, EXIT_STATUS
from cellprofiler_core.measurement import Measurements
from cellprofiler_core.object import ObjectSet
from cellprofiler_core.pipeline import LoadException
from cellprofiler_core.pipeline import Pipeline
from cellprofiler_core.preferences import get_image_set_file
from cellprofiler_core.preferences import get_temporary_directory
from cellprofiler_core.preferences import set_conserve_memory
#TODO: disabled until CellProfiler/CellProfiler#4684 is resolved
# from cellprofiler_core.preferences import get_omero_port
# from cellprofiler_core.preferences import get_omero_server
# from cellprofiler_core.preferences import get_omero_session_id
# from cellprofiler_core.preferences import get_omero_user
from cellprofiler_core.preferences import set_allow_schema_write
from cellprofiler_core.preferences import set_always_continue
from cellprofiler_core.preferences import set_awt_headless
from cellprofiler_core.preferences import set_data_file
from cellprofiler_core.preferences import set_default_image_directory
from cellprofiler_core.preferences import set_default_output_directory
from cellprofiler_core.preferences import set_headless
from cellprofiler_core.preferences import set_image_set_file
#TODO: disabled until CellProfiler/CellProfiler#4684 is resolved
# from cellprofiler_core.preferences import set_omero_port
# from cellprofiler_core.preferences import set_omero_server
# from cellprofiler_core.preferences import set_omero_user
from cellprofiler_core.preferences import set_plugin_directory
from cellprofiler_core.preferences import set_temporary_directory
from cellprofiler_core.preferences import set_widget_inspector
from cellprofiler_core.utilities.core.workspace import is_workspace_file
from cellprofiler_core.utilities.hdf5_dict import HDF5FileList
from cellprofiler_core.utilities.java import start_java, stop_java
from cellprofiler_core.utilities.measurement import load_measurements
from cellprofiler_core.utilities.zmq import join_to_the_boundary
from cellprofiler_core.utilities.logging import set_log_level
from cellprofiler_core.worker import aw_parse_args
from cellprofiler_core.worker import main as worker_main
from cellprofiler_core.workspace import Workspace
from cellprofiler_core.reader import fill_readers, builtin_readers, filter_active_readers, AVAILABLE_READERS

LOGGER = logging.getLogger(__name__)

if hasattr(sys, "frozen"):
    if sys.platform == "darwin":
        # Some versions of Macos like to put CP in a sandbox. If we're frozen Java should be packed in,
        # so let's just figure out the directory at run time.
        try:
            os.environ["CP_JAVA_HOME"] = os.path.abspath(os.path.join(sys.prefix, "..", "Resources/Home"))
        except:
            print("Unable to set JAVA directory to inbuilt java environment")
    elif sys.platform.startswith("win"):
        # Clear out deprecation warnings from PyInstaller
        os.system('cls')
        # For Windows builds use built-in Java for CellProfiler, otherwise try to use Java from elsewhere on the system.
        # Users can use a custom java installation by removing CP_JAVA_HOME.
        # JAVA_HOME must be set before bioformats import.
        try:
            if "CP_JAVA_HOME" in os.environ:
                # Use user-provided Java
                os.environ["JAVA_HOME"] = os.environ["CP_JAVA_HOME"]
            elif "JAVA_HOME" not in os.environ:
                # Use built-in java
                test_dir = os.path.abspath(os.path.join(sys.prefix, "java"))
                if os.path.exists(test_dir):
                    os.environ["JAVA_HOME"] = test_dir
                else:
                    print(f"Failed to detect java automatically. Searched in: {test_dir}.")
            assert "JAVA_HOME" in os.environ and os.path.exists(os.environ['JAVA_HOME'])
            # Ensure we start in the correct directory when launching a build.
            # Opening a file directly may end up with us starting on the wrong drive.
            os.chdir(sys.prefix)
        except AssertionError:
            print(
                "CellProfiler Startup ERROR: Could not find path to Java environment directory.\n"
                "Please set the CP_JAVA_HOME system environment variable.\n"
                "Visit http://broad.io/cpjava for instructions."
            )
            os.system("pause")  # Keep console window open until keypress.
            sys.exit(1)
        except Exception as e:
            print(f"Encountered unknown error during startup: {e}")
    else:
        # Clear out deprecation warnings from PyInstaller
        os.system('clear')
    print(f"Starting CellProfiler {cellprofiler_version}")


#TODO: disabled until CellProfiler/CellProfiler#4684 is resolved
# OMERO_CK_HOST = "host"
# OMERO_CK_PORT = "port"
# OMERO_CK_USER = "user"
# OMERO_CK_PASSWORD = "password"
# OMERO_CK_SESSION_ID = "session-id"
# OMERO_CK_CONFIG_FILE = "config-file"

numpy.seterr(all="ignore")


def main(main_args=None):
    """Run CellProfiler

    args - command-line arguments, e.g., sys.argv
    """
    if main_args is None:
        main_args = sys.argv

    set_awt_headless(True)

    exit_code = 0

    switches = ("--analysis-id", "--work-server", "--knime-bridge-address")

    if any([any([arg.startswith(switch) for switch in switches]) for arg in main_args]):
        set_headless()
        aw_parse_args()
        fill_readers(check_config=True)
        worker_main()
        return exit_code

    args = parse_args(main_args)

    # put up towards top, some things below need log level set
    set_log_level(args.log_level)

    if args.print_version:
        set_headless()
        __version__(exit_code)

    if (not args.show_gui) or args.write_schema_and_exit:
        set_headless()

        args.run_pipeline = True

    if args.batch_commands_file or args.new_batch_commands_file:
        set_headless()
        args.run_pipeline = False
        args.show_gui = False

    # must be run after last possible invocation of set_headless()
    fill_readers(check_config=True)

    if args.temp_dir is not None:
        if not os.path.exists(args.temp_dir):
            os.makedirs(args.temp_dir)
        set_temporary_directory(args.temp_dir, globally=False)

    temp_dir = get_temporary_directory()

    to_clean = []

    if args.pipeline_filename:
        o = urllib.parse.urlparse(args.pipeline_filename)
        if o[0] in ("ftp", "http", "https"):
            from urllib.request import urlopen

            temp_pipe_file = tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".cppipe", dir=temp_dir, delete=False
            )
            downloaded_pipeline = urlopen(args.pipeline_filename)
            for line in downloaded_pipeline:
                temp_pipe_file.write(line)
            args.pipeline_filename = temp_pipe_file.name
            to_clean.append(os.path.join(temp_dir, temp_pipe_file.name))

    if args.image_set_file:
        o = urllib.parse.urlparse(args.image_set_file)
        if o[0] in ("ftp", "http", "https"):
            from urllib.request import urlopen

            temp_set_file = tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".csv", dir=temp_dir, delete=False
            )
            downloaded_set_csv = urlopen(args.image_set_file)
            for line in downloaded_set_csv:
                temp_set_file.write(line)
            args.image_set_file = temp_set_file.name
            to_clean.append(os.path.join(temp_dir, temp_set_file.name))

    if args.data_file:
        o = urllib.parse.urlparse(args.data_file)
        if o[0] in ("ftp", "http", "https"):
            from urllib.request import urlopen

            temp_data_file = tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".csv", dir=temp_dir, delete=False
            )
            downloaded_data_csv = urlopen(args.data_file)
            for line in downloaded_data_csv:
                temp_data_file.write(line)
            args.data_file = temp_data_file.name
            to_clean.append(os.path.join(temp_dir, temp_data_file.name))

    if args.print_groups_file is not None:
        print_groups(args.print_groups_file)

    if args.batch_commands_file is not None:
        try:
            nr_per_batch = int(args.images_per_batch)
        except ValueError:
            LOGGER.warning(
                "non-integer argument to --images-per-batch. Defaulting to 1."
            )
            nr_per_batch = 1
        get_batch_commands(args.batch_commands_file, nr_per_batch)
    
    if args.new_batch_commands_file is not None:
        try:
            nr_per_batch = int(args.images_per_batch)
        except ValueError:
            LOGGER.warning(
                "non-integer argument to --images-per-batch. Defaulting to 1."
            )
            nr_per_batch = 1
        get_batch_commands_new(args.new_batch_commands_file, nr_per_batch)

    #TODO: disabled until CellProfiler/CellProfiler#4684 is resolved
    # if args.omero_credentials is not None:
    #     set_omero_credentials_from_string(args.omero_credentials)

    if args.plugins_directory is not None:
        set_plugin_directory(args.plugins_directory, globally=False)

    if args.conserve_memory is not None:
        set_conserve_memory(args.conserve_memory, globally=False)

    if args.enabled_readers is not None and len(args.enabled_readers) != 0:
        filter_active_readers(args.enabled_readers)

    if args.always_continue is not None:
        set_always_continue(args.always_continue, globally=False)

    if not args.allow_schema_write:
        set_allow_schema_write(False)

    if args.output_directory:
        if not os.path.exists(args.output_directory):
            os.makedirs(args.output_directory)

        set_default_output_directory(args.output_directory)

    if args.image_directory:
        set_default_image_directory(args.image_directory)

    if args.run_pipeline and not args.pipeline_filename:
        raise ValueError("You must specify a pipeline filename to run")

    if args.data_file is not None:
        set_data_file(os.path.abspath(args.data_file))

    if args.widget_inspector:
        set_widget_inspector(True, globally=False)

    try:

        if args.image_set_file is not None:
            set_image_set_file(args.image_set_file)

        #
        # Handle command-line tasks that that need to load the modules to run
        #
        if args.print_measurements:
            print_measurements(args)

        if args.write_schema_and_exit:
            write_schema(args.pipeline_filename)

        if args.show_gui:
            matplotlib.use("WXAgg")

            import cellprofiler.gui.app

            if args.pipeline_filename:
                if is_workspace_file(args.pipeline_filename):
                    workspace_path = os.path.expanduser(args.pipeline_filename)

                    pipeline_path = None
                else:
                    pipeline_path = os.path.expanduser(args.pipeline_filename)

                    workspace_path = None
            else:
                workspace_path = None

                pipeline_path = None

            app = cellprofiler.gui.app.App(
                0, workspace_path=workspace_path, pipeline_path=pipeline_path
            )
            if args.image_directory is not None:
                plc = app.frame.get_pipeline_controller()
                plc.add_paths_to_pathlist([args.image_directory])

            if args.run_pipeline:
                app.frame.pipeline_controller.do_analyze_images()

            app.MainLoop()

            return
        elif args.run_pipeline:
            exit_code = run_pipeline_headless(args)

    finally:
        # Cleanup the temp files we made, if any
        if len(to_clean) > 0:
            for each_temp in to_clean:
                os.remove(each_temp)
        # If anything goes wrong during the startup sequence headlessly, the JVM needs
        # to be explicitly closed
        if not args.show_gui:
            stop_cellprofiler()

    return exit_code


def __version__(exit_code):
    print(cellprofiler_version)

    sys.exit(exit_code)


def stop_cellprofiler():

    # Bioformats readers have to be properly closed.
    # This is especially important when using OmeroReaders as leaving the
    # readers open leaves the OMERO.server services open which in turn leads to
    # high memory consumption.
    from cellprofiler_core.constants.reader import ALL_READERS
    for reader in ALL_READERS.values():
        reader.clear_cached_readers()
    stop_java()


def parse_args(args):
    """Parse the CellProfiler command-line arguments"""

    # https://stackoverflow.com/a/22157136
    class SmartFormatter(argparse.HelpFormatter):
        def _split_lines(self, text, width):
            if text.startswith('R|'):
                return text[2:].splitlines()  
            # this is the RawTextHelpFormatter._split_lines
            return argparse.HelpFormatter._split_lines(self, text, width)

    usage = """%(prog)s [arguments]
         The flags -p, -r and -c are required for a headless run."""

    if "--do-not-fetch" in args:
        args = list(args)

        args.remove("--do-not-fetch")

    parser = argparse.ArgumentParser(usage=usage, prog="cellprofiler", formatter_class=SmartFormatter)

    parser.add_argument(
        "-p",
        "--pipeline",
        "--project",
        dest="pipeline_filename",
        help="Load this pipeline file or project on startup. If specifying a pipeline file rather than a project, the -i flag is also needed unless the pipeline is saved with the file list.",
        default=None,
    )

    default_show_gui = True

    if sys.platform.startswith("linux") and not os.getenv("DISPLAY"):
        default_show_gui = False

    parser.add_argument(
        "-c",
        "--run-headless",
        action="store_false",
        dest="show_gui",
        default=default_show_gui,
        help="Run headless (without the GUI)",
    )

    parser.add_argument(
        "-r",
        "--run",
        action="store_true",
        dest="run_pipeline",
        default=False,
        help="Run the given pipeline on startup",
    )

    parser.add_argument(
        "-o",
        "--output-directory",
        dest="output_directory",
        default=None,
        help="Make this directory the default output folder",
    )

    parser.add_argument(
        "-i",
        "--image-directory",
        dest="image_directory",
        default=None,
        help="Make this directory the default input folder",
    )

    parser.add_argument(
        "-f",
        "--first-image-set",
        dest="first_image_set",
        default=None,
        help="The one-based index of the first image set to process",
    )

    parser.add_argument(
        "-l",
        "--last-image-set",
        dest="last_image_set",
        default=None,
        help="The one-based index of the last image set to process",
    )

    parser.add_argument(
        "-g",
        "--group",
        dest="groups",
        default=None,
        help='Restrict processing to one grouping in a grouped pipeline. For instance, "-g ROW=H,COL=01", will process only the group of image sets that match the keys.',
    )

    parser.add_argument(
        "--plugins-directory",
        dest="plugins_directory",
        help="CellProfiler will look for plugin modules in this directory (headless-only).",
    )

    parser.add_argument(
        "--conserve-memory",
        dest="conserve_memory",
        default=None,
        help="CellProfiler will attempt to release unused memory after each image set.",
    )

    valid_reader_choices = "\n\t".join(builtin_readers.keys())
    parser.add_argument(
        "-e",
        "--enable-readers",
        dest="enabled_readers",
        nargs="*",
        help="R|A space delimited list of readers to enable.\n"
            "If none specified, ALL will be enabled.\n"
            f"Valid choices:\n\t{valid_reader_choices}"
    )

    parser.add_argument(
        "--version",
        dest="print_version",
        default=False,
        action="store_true",
        help="Print the version and exit",
    )

    parser.add_argument(
        "-t",
        "--temporary-directory",
        dest="temp_dir",
        default=None,
        help=(
            "Temporary directory. "
            "CellProfiler uses this for downloaded image files "
            "and for the measurements file, if not specified. "
            "The default is " + tempfile.gettempdir()
        ),
    )

    parser.add_argument(
        "-d",
        "--done-file",
        dest="done_file",
        default=None,
        help='The path to the "Done" file, written by CellProfiler shortly before exiting',
    )

    parser.add_argument(
        "--measurements",
        dest="print_measurements",
        default=False,
        action="store_true",
        help="Open the pipeline file specified by the -p switch and print the measurements made by that pipeline",
    )

    parser.add_argument(
        "--print-groups",
        dest="print_groups_file",
        default=None,
        help="Open the measurements file following the --print-groups switch and print the groups in its image sets. The measurements file should be generated using CreateBatchFiles. The output is a JSON-encoded data structure containing the group keys and values and the image sets in each group.",
    )

    parser.add_argument(
        "--get-batch-commands",
        dest="batch_commands_file",
        default=None,
        help='Open the measurements file following the --get-batch-commands switch and print one line to the console per group. The measurements file should be generated using CreateBatchFiles and the image sets should be grouped into the units to be run. Each line is a command to invoke CellProfiler. You can use this option to generate a shell script that will invoke CellProfiler on a cluster by substituting "CellProfiler" '
        "with your invocation command in the script's text, for instance: CellProfiler --get-batch-commands Batch_data.h5 | sed s/CellProfiler/farm_jobs.sh. Note that CellProfiler will always run in headless mode when --get-batch-commands is present and will exit after generating the batch commands without processing any pipeline. Note that this exact version is deprecated and will be removed in CellProfiler 5; you may use the new version now with --get-batch-commands-new",
    )

    parser.add_argument(
        "--get-batch-commands-new",
        dest="new_batch_commands_file",
        default=None,
        help='Open the batch file following the --get-batch-commands-new switch and print one line to the console per group. Each line is a command to invoke CellProfiler. You can use this option to generate a shell script that will invoke CellProfiler on a cluster by substituting "CellProfiler". This new version (which will be the only version in CellProfiler 5) will return groups if CellProfiler has more than one group and --images-per-batch is NOT passed (or is passed as 1), otherwise it will always return -f and -l commands. '
        "with your invocation command in the script's text, for instance: CellProfiler --get-batch-commands-new Batch_data.h5 | sed s/CellProfiler/farm_jobs.sh. Note that CellProfiler will always run in headless mode when --get-batch-commands is present and will exit after generating the batch commands without processing any pipeline.",
    )

    parser.add_argument(
        "--images-per-batch",
        dest="images_per_batch",
        default="1",
        help="For pipelines that do not use image grouping this option specifies the number of images that should be processed in each batch if --get-batch-commands is used. Defaults to 1.",
    )

    parser.add_argument(
        "--data-file",
        dest="data_file",
        default=None,
        help="Specify the location of a .csv file for LoadData. If this switch is present, this file is used instead of the one specified in the LoadData module.",
    )

    parser.add_argument(
        "--file-list",
        dest="image_set_file",
        default=None,
        help="Specify a file list of one file or URL per line to be used to initially populate the Images module's file list.",
    )

    parser.add_argument(
        "--do-not-write-schema",
        dest="allow_schema_write",
        default=True,
        action="store_false",
        help="Do not execute the schema definition and other per-experiment SQL commands during initialization when running a pipeline in batch mode.",
    )

    parser.add_argument(
        "--write-schema-and-exit",
        dest="write_schema_and_exit",
        default=False,
        action="store_true",
        help="Create the experiment database schema and exit",
    )

    #TODO: disabled until CellProfiler/CellProfiler#4684 is resolved
    # parser.add_argument(
    #     "--omero-credentials",
    #     dest="omero_credentials",
    #     default=None,
    #     help=(
    #         "Enter login credentials for OMERO. The credentials"
    #         " are entered as comma-separated key/value pairs with"
    #         ' keys, "%(OMERO_CK_HOST)s" - the DNS host name for the OMERO server'
    #         ', "%(OMERO_CK_PORT)s" - the server\'s port # (typically 4064)'
    #         ', "%(OMERO_CK_USER)s" - the name of the connecting user'
    #         ', "%(OMERO_CK_PASSWORD)s" - the connecting user\'s password'
    #         ', "%(OMERO_CK_SESSION_ID)s" - the session ID for an OMERO client session.'
    #         ', "%(OMERO_CK_CONFIG_FILE)s" - the path to the OMERO credentials config file.'
    #         " A typical set of credentials might be:"
    #         " --omero-credentials host=demo.openmicroscopy.org,port=4064,session-id=atrvomvjcjfe7t01e8eu59amixmqqkfp"
    #     )
    #     % globals(),
    # )

    parser.add_argument(
        "-L",
        "--log-level",
        dest="log_level",
        default=str(logging.INFO),
        help=(
            "Set the verbosity for logging messages: "
            + ("%d or %s for debugging, " % (logging.DEBUG, "DEBUG"))
            + ("%d or %s for informational, " % (logging.INFO, "INFO"))
            + ("%d or %s for warning, " % (logging.WARNING, "WARNING"))
            + ("%d or %s for error, " % (logging.ERROR, "ERROR"))
            + ("%d or %s for critical, " % (logging.CRITICAL, "CRITICAL"))
            + ("%d or %s for fatal." % (logging.FATAL, "FATAL"))
            + " Otherwise, the argument is interpreted as the file name of a log configuration file (see http://docs.python.org/library/logging.config.html for file format)"
        ),
    )

    parser.add_argument(
        "--always-continue",
        dest="always_continue",
        default=None,
        action="store_true",
        help="Keep running after an image set throws an error"
    )

    parser.add_argument(
        "--widget-inspector",
        dest="widget_inspector",
        default=False,
        action="store_true",
        help="Enable the widget inspector menu item under \"Test\""
    )

    parsed_args = parser.parse_args()

    return parsed_args

#TODO: disabled until CellProfiler/CellProfiler#4684 is resolved
# def set_omero_credentials_from_string(credentials_string):
#     """Set the OMERO server / port / session ID

#     credentials_string: a comma-separated key/value pair string (key=value)
#                         that gives the credentials. Keys are
#                         host - the DNS name or IP address of the OMERO server
#                         port - the TCP port to use to connect
#                         user - the user name
#                         session-id - the session ID used for authentication
#     """
#     from cellprofiler_core.bioformats import formatreader

#     if re.match("([^=^,]+=[^=^,]+,)*([^=^,]+=[^=^,]+)", credentials_string) is None:
#         logging.root.error(
#             'The OMERO credentials string, "%s", is badly-formatted.'
#             % credentials_string
#         )

#         logging.root.error(
#             'It should have the form: "host=hostname.org,port=####,user=<user>,session-id=<session-id>\n'
#         )

#         raise ValueError("Invalid format for --omero-credentials")

#     credentials = {}

#     for k, v in [kv.split("=", 1) for kv in credentials_string.split(",")]:
#         k = k.lower()

#         credentials = {
#             formatreader.K_OMERO_SERVER: get_omero_server(),
#             formatreader.K_OMERO_PORT: get_omero_port(),
#             formatreader.K_OMERO_USER: get_omero_user(),
#             formatreader.K_OMERO_SESSION_ID: get_omero_session_id(),
#         }

#         if k == OMERO_CK_HOST:
#             set_omero_server(v, globally=False)

#             credentials[formatreader.K_OMERO_SERVER] = v
#         elif k == OMERO_CK_PORT:
#             set_omero_port(v, globally=False)

#             credentials[formatreader.K_OMERO_PORT] = v
#         elif k == OMERO_CK_SESSION_ID:
#             credentials[formatreader.K_OMERO_SESSION_ID] = v
#         elif k == OMERO_CK_USER:
#             set_omero_user(v, globally=False)

#             credentials[formatreader.K_OMERO_USER] = v
#         elif k == OMERO_CK_PASSWORD:
#             credentials[formatreader.K_OMERO_PASSWORD] = v
#         elif k == OMERO_CK_CONFIG_FILE:
#             credentials[formatreader.K_OMERO_CONFIG_FILE] = v

#             if not os.path.isfile(v):
#                 msg = "Cannot find OMERO config file, %s" % v

#                 logging.root.error(msg)

#                 raise ValueError(msg)
#         else:
#             logging.root.error('Unknown --omero-credentials keyword: "%s"' % k)

#             logging.root.error(
#                 'Acceptable keywords are: "%s"'
#                 % '","'.join([OMERO_CK_HOST, OMERO_CK_PORT, OMERO_CK_SESSION_ID])
#             )

#             raise ValueError("Invalid format for --omero-credentials")

#     formatreader.use_omero_credentials(credentials)


def print_measurements(args):
    """Print the measurements that would be output by a pipeline

    This function calls Pipeline.get_measurement_columns() to get the
    measurements that would be output by a pipeline. This can be used in
    a workflow tool or LIMS to find the outputs of a pipeline without
    running it. For instance, someone might want to integrate CellProfiler
    with Knime and write a Knime node that let the user specify a pipeline
    file. The node could then execute CellProfiler with the --measurements
    switch and display the measurements as node outputs.
    """

    if args.pipeline_filename is None:
        raise ValueError("Can't print measurements, no pipeline file")

    pipeline = Pipeline()

    def callback(pipeline, event):
        if isinstance(event, LoadException):
            raise ValueError("Failed to load %s" % args.pipeline_filename)

    pipeline.add_listener(callback)

    pipeline.load(os.path.expanduser(args.pipeline_filename))

    columns = pipeline.get_measurement_columns()

    print("--- begin measurements ---")

    print("Object,Feature,Type")

    for column in columns:
        object_name, feature, data_type = column[:3]

        print("%s,%s,%s" % (object_name, feature, data_type))

    print("--- end measurements ---")


def print_groups(filename):
    """
    Print the image set groups for this pipeline

    This function outputs a JSON string to the console composed of a list
    of the groups in the pipeline image set. Each element of the list is
    a two-tuple whose first element is a key/value dictionary of the
    group's key and the second is a tuple of the image numbers in the group.
    """
    path = os.path.expanduser(filename)

    m = Measurements(filename=path, mode="r")

    metadata_tags = m.get_grouping_tags_or_metadata()

    groupings = m.get_groupings(metadata_tags)

    # Groupings are np.int64 which cannot be dumped to json
    groupings_export = []
    for g in groupings:
        groupings_export.append((g[0], [int(imgnr) for imgnr in g[1]]))

    json.dump(groupings_export, sys.stdout)


def get_batch_commands(filename, n_per_job=1):
    """Print the commands needed to run the given batch data file headless

    filename - the name of a Batch_data.h5 file. The file should group image sets.

    The output assumes that the executable, "CellProfiler", can be used
    to run the command from the shell. Alternatively, the output could be
    run through a utility such as "sed":

    CellProfiler --get-batch-commands Batch_data.h5 | sed s/CellProfiler/farm_job.sh/
    """
    path = os.path.expanduser(filename)

    m = Measurements(filename=path, mode="r")

    image_numbers = m.get_image_numbers()

    if m.has_feature(IMAGE, GROUP_NUMBER):
        group_numbers = m[
            IMAGE, GROUP_NUMBER, image_numbers,
        ]

        group_indexes = m[
            IMAGE, GROUP_INDEX, image_numbers,
        ]

        if numpy.any(group_numbers != 1) and numpy.all(
            (group_indexes[1:] == group_indexes[:-1] + 1)
            | ((group_indexes[1:] == 1) & (group_numbers[1:] == group_numbers[:-1] + 1))
        ):
            #
            # Do -f and -l if more than one group and group numbers
            # and indices are properly constructed
            #
            bins = numpy.bincount(group_numbers)

            cumsums = numpy.cumsum(bins)

            prev = 0

            for i, off in enumerate(cumsums):
                if off == prev:
                    continue

                print(
                    "CellProfiler -c -r -p %s -f %d -l %d" % (filename, prev + 1, off)
                )

                prev = off
    else:
        metadata_tags = m.get_grouping_tags_or_metadata()

        if len(metadata_tags) == 1 and metadata_tags[0] == "ImageNumber":
            for i in range(0, len(image_numbers), n_per_job):
                first = image_numbers[i]
                last = image_numbers[min(i + n_per_job - 1, len(image_numbers) - 1)]
                print("CellProfiler -c -r -p %s -f %d -l %d" % (filename, first, last))
        else:
            # LoadData w/ images grouped by metadata tags
            groupings = m.get_groupings(metadata_tags)

            for grouping in groupings:
                group_string = ",".join(
                    ["%s=%s" % (k, v) for k, v in list(grouping[0].items())]
                )

                print("CellProfiler -c -r -p %s -g %s" % (filename, group_string))
    return

def get_batch_commands_new(filename, n_per_job=1):
    """Print the commands needed to run the given batch data file headless

    filename - the name of a Batch_data.h5 file. The file may (but need not) group image sets.

    You can explicitly set the batch size with --images-per-batch, but note that
    it will override existing groupings, so use with caution

    The output assumes that the executable, "CellProfiler", can be used
    to run the command from the shell. Alternatively, the output could be
    run through a utility such as "sed":

    CellProfiler --get-batch-commands Batch_data.h5 | sed s/CellProfiler/farm_job.sh/
    """
    path = os.path.expanduser(filename)

    m = Measurements(filename=path, mode="r")

    image_numbers = m.get_image_numbers()

    grouping_tags = m.get_grouping_tags_only()

    if n_per_job != 1 or grouping_tags == []:
        # One of two things is happening:
        # 1) We've manually set a batch size, and we should always obey it, even if there was grouping
        # 2) There was no grouping so our only choice is to use -f -l
        for i in range(0, len(image_numbers), n_per_job):
            first = image_numbers[i]
            last = image_numbers[min(i + n_per_job - 1, len(image_numbers) - 1)]
            print("CellProfiler -c -r -p %s -f %d -l %d" % (filename, first, last))
    
    else: #We have grouping enabled and haven't overriden it
        groupings = m.get_groupings(grouping_tags)
        for grouping in groupings:
            group_string = ",".join(
                ["%s=%s" % (k, v) for k, v in list(grouping[0].items())]
            )

            print("CellProfiler -c -r -p %s -g %s" % (filename, group_string))

    return


def write_schema(pipeline_filename):
    if pipeline_filename is None:
        raise ValueError(
            "The --write-schema-and-exit switch must be used in conjunction\nwith the -p or --pipeline switch to load a pipeline with an\n"
            "ExportToDatabase module."
        )

    pipeline = Pipeline()

    pipeline.load(pipeline_filename)

    pipeline.turn_off_batch_mode()

    for module in pipeline.modules():
        if module.module_name == "ExportToDatabase":
            break
    else:
        raise ValueError(
            'The pipeline, "%s", does not have an ExportToDatabase module'
            % pipeline_filename
        )

    m = Measurements()

    workspace = Workspace(pipeline, module, m, ObjectSet, m, None)

    module.prepare_run(workspace)


def run_pipeline_headless(args):
    """
    Run a CellProfiler pipeline in headless mode
    """
    if args.first_image_set is not None:
        if not args.first_image_set.isdigit():
            raise ValueError("The --first-image-set option takes a numeric argument")
        else:
            image_set_start = int(args.first_image_set)
    else:
        image_set_start = 1

    image_set_numbers = None

    if args.last_image_set is not None:
        if not args.last_image_set.isdigit():
            raise ValueError("The --last-image-set option takes a numeric argument")
        else:
            image_set_end = int(args.last_image_set)

            if image_set_start is None:
                image_set_numbers = numpy.arange(1, image_set_end + 1)
            else:
                image_set_numbers = numpy.arange(image_set_start, image_set_end + 1)
    else:
        image_set_end = None

    if (args.pipeline_filename is not None) and (
        not args.pipeline_filename.lower().startswith("http")
    ):
        args.pipeline_filename = os.path.expanduser(args.pipeline_filename)

    pipeline = Pipeline()

    initial_measurements = None

    try:
        if h5py.is_hdf5(args.pipeline_filename):
            initial_measurements = load_measurements(
                args.pipeline_filename, image_numbers=image_set_numbers
            )
    except:
        logging.root.info("Failed to load measurements from pipeline")

    if initial_measurements is not None:
        pipeline_text = initial_measurements.get_experiment_measurement(M_PIPELINE)

        pipeline_text = pipeline_text

        pipeline.load(io.StringIO(pipeline_text))

        if not pipeline.in_batch_mode():
            #
            # Need file list in order to call prepare_run
            #

            with h5py.File(args.pipeline_filename, "r") as src:
                if HDF5FileList.has_file_list(src):
                    HDF5FileList.copy(src, initial_measurements.hdf5_dict.hdf5_file)
    else:
        pipeline.load(args.pipeline_filename)

    if args.groups is not None:
        kvs = [x.split("=") for x in args.groups.split(",")]

        groups = dict(kvs)
    else:
        groups = None

    file_list = get_image_set_file()

    if file_list is not None:
        pipeline.read_file_list(file_list)
    elif args.image_directory is not None:
        pathnames = []

        for dirname, _, fnames in os.walk(os.path.abspath(args.image_directory)):
            pathnames.append(
                [
                    os.path.join(dirname, fname)
                    for fname in fnames
                    if os.path.isfile(os.path.join(dirname, fname))
                ]
            )

        pathnames = sum(pathnames, [])

        pipeline.add_pathnames_to_file_list(pathnames)

    #
    # Fixup CreateBatchFiles with any command-line input or output directories
    #
    if pipeline.in_batch_mode():
        create_batch_files = [
            m for m in pipeline.modules() if m.is_create_batch_module()
        ]

        if len(create_batch_files) > 0:
            create_batch_files = create_batch_files[0]

            if args.output_directory is not None:
                create_batch_files.custom_output_directory.value = (
                    args.output_directory
                )

            if args.image_directory is not None:
                create_batch_files.default_image_directory.value = (
                    args.image_directory
                )

    measurements = pipeline.run(
        image_set_start=image_set_start,
        image_set_end=image_set_end,
        grouping=groups,
        measurements_filename=None,
        initial_measurements=initial_measurements,
    )

    if args.done_file is not None:
        if measurements is not None and measurements.has_feature(
            EXPERIMENT, EXIT_STATUS,
        ):
            done_text = measurements.get_experiment_measurement(EXIT_STATUS)

            exit_code = 0 if done_text == "Complete" else -1
        else:
            done_text = "Failure"

            exit_code = -1

        fd = open(args.done_file, "wt")
        fd.write("%s\n" % done_text)
        fd.close()
    elif not measurements.has_feature(EXPERIMENT, EXIT_STATUS):
        # The pipeline probably failed
        exit_code = 1
    else:
        exit_code = 0

    if measurements is not None:
        measurements.close()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
