#!/usr/bin/env python

"""
Command Line Interface for running the DSBox TA2 Search
"""

from dsbox_dev_setup import path_setup
path_setup()

import sys
import os
import json
import signal
import traceback

from dsbox.planner.controller import Controller, Feature
from dsbox.planner.event_handler import PlannerEventHandler

# import pydevd
# pydevd.settrace('76.174.187.118')
#pydevd.settrace('128.9.184.183')
#pydevd.settrace('128.9.128.37')


TIMEOUT = 25*60 # Timeout after 25 minutes

DEBUG = 1

LIB_DIRECTORY = os.path.dirname(os.path.realpath(__file__)) + "/library"

def main(argv=None): # IGNORE:C0111
    '''Command line options.'''

    if argv is None:
        argv = sys.argv
    else:
        sys.argv.extend(argv)

    program_name = os.path.basename(sys.argv[0])
    program_shortdesc = __import__('__main__').__doc__.split("\n")[1]
    program_usage = '''%s
USAGE
ta2-search <search_config_file>
''' % program_shortdesc

    if len(sys.argv) < 2:
        print(program_usage)
        exit(1)

    conf_file = sys.argv[1]
    config = {}
    with open(conf_file) as conf_data:
        config = json.load(conf_data)
        conf_data.close()

    if "timeout" in config:
        # Timeout less 60 seconds, to give system chance to clean up
        TIMEOUT = int(config.get("timeout"))*60 - 60

    # Start the controller
    controller = Controller(LIB_DIRECTORY, development_mode=DEBUG>0)
    controller.initialize_from_config(config)
    controller.load_problem()

    # Setup a signal handler to exit gracefully
    # Either on an interrupt or after a certain time
    def write_results_and_exit(a_signal, frame):
        print('SIGNAL exit: {}'.format(conf_file))
        try:
            # Reset to handlers to default as not to output multiple times
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            print('SIGNAL exit done reset signal {}'.format(conf_file))

            controller.write_training_results()
            print('SIGNAL exit done writing: {}'.format(conf_file), flush=True)
        except Exception as e:
            print(e)
            traceback.print_exc()

        # sys.exit(0) generates SystemExit exception, which may
        # be caught and ignore.

        # This os._exit() cannot be caught.
        print('SIGNAL exiting {}'.format(conf_file), flush=True)
        os._exit(0)

    if not DEBUG:
        signal.signal(signal.SIGINT, write_results_and_exit)
        signal.signal(signal.SIGTERM, write_results_and_exit)
        signal.signal(signal.SIGALRM, write_results_and_exit)
        signal.alarm(TIMEOUT)

    # Load in data
    controller.initialize_training_data_from_config()

    # Start training
    controller.initialize_planners()
    for result in controller.train(PlannerEventHandler(), timeout=TIMEOUT):
        if result == False:
            print("ProblemNotImplemented")
            sys.exit(148)
        pass

    print('exit: {}'.format(conf_file))

if __name__ == "__main__":
    if DEBUG:
        sys.argv.append("-h")
        sys.argv.append("-v")
    sys.exit(main())
