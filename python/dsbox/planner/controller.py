import asyncio
import copy
import json
import functools
import os
import sys
import traceback
import pdb

import pandas as pd

from typing import List

from collections import defaultdict

from dsbox.planner.leveltwo.l1proxy import LevelOnePlannerProxy
from dsbox.planner.leveltwo.planner import LevelTwoPlanner
from dsbox.schema.data_profile import DataProfile
from dsbox.executer.executionhelper import ExecutionHelper
from dsbox.planner.common.data_manager import Dataset, DataManager
from dsbox.planner.common.pipeline import Pipeline, PipelineExecutionResult, OneStandardErrorPipelineSorter, PipelineSorter
from dsbox.planner.common.problem_manager import Problem
from dsbox.planner.common.resource_manager import ResourceManager
from dsbox.planner.ensemble import Ensemble

from dsbox.planner.hyperparam_tuning import RandomHyperparamTuning

NUMBER_HYPERPARAM_SEARCHES = 100

class Feature:
    def __init__(self, resource_id, feature_name):
        self.resource_id = resource_id
        self.feature_name = feature_name

class ExecutionCache:
    def __init__(self):
        self.l1_pipelines_handled = {}
        self.l2_pipelines_handled = {}
        self.l2_pipelines_map = {}

        self.l2_l1_map = {}

class Controller(object):
    problem = None
    dataset = None
    execution_helper = None
    resource_manager = None

    config = None
    num_cpus = 0
    ram = 0
    timeout = 60
    include = []
    exclude = []
    #max_ensemble = 5

    exec_pipelines = []
    l1_planner = None
    l2_planner = None

    """
    This is the overall "planning" coordinator. It is passed in the data directory
    and the primitives library directory, and it generates plans by calling out to L1, L2
    and L3 planners.
    """
    def __init__(self, libdir, development_mode=False):
        # FIXME: This should change to the primitive discovery interface
        self.libdir = os.path.abspath(libdir)
        self.development_mode = development_mode

        self.cache = ExecutionCache()
        self.tuner = RandomHyperparamTuning()

        # Planner event handler
        self.pe = None

        # All pipelines that have finished executing
        self.exec_pipelines = []

        self.log_dir = None
        self.exec_dir = None
        self.tmp_dir = None
        self.problem = None
        self.data_manager = None
        self.logfile = None
        self.errorfile = None
        self.pipelinesfile = None

        self.hyperparam_count = 0

        self.exec_pipelines: List[Pipeline] = []
        self._pipeline_sorter: PipelineSorter = None

        self.exec_pipelines: List[Pipeline] = []
        self._pipeline_sorter: PipelineSorter = None

    '''
    Set config directories and data schema file
    '''
    def initialize_from_config(self, config):
        self.config = config

        self.log_dir = self._dir(config, 'pipeline_logs_root', True)
        self.exec_dir = self._dir(config, 'executables_root', True)
        self.tmp_dir = self._dir(config, 'temp_storage_root', True)

        self.num_cpus = int(config.get('cpus', 0))
        self.ram = config.get('ram', 0)
        self.timeout = (config.get('timeout', 60))*60
        #self.max_ensemble = int(config.get('max_ensemble', 0))

        # Create some debugging files
        self.logfile = open("%s%slog.txt" % (self.tmp_dir, os.sep), 'w')
        self.errorfile = open("%s%sstderr.txt" % (self.tmp_dir, os.sep), 'w')
        self.pipelinesfile = open("%s%spipelines.txt" % (self.tmp_dir, os.sep), 'w')
        self.statistics_filename = "%s%sstatistics.jsonl" % (self.tmp_dir, os.sep)
        self.test_pipelinesfile = open("%s%stest_pipelines.txt" % (self.tmp_dir, os.sep), 'w')

        self.problem = Problem()
        self.data_manager = DataManager()
        self.execution_helper = ExecutionHelper(self.problem, self.data_manager)
        self.resource_manager = ResourceManager(self.execution_helper, self.num_cpus)

        if not self.development_mode:
            # Redirect stderr to error file
            sys.stderr = self.errorfile
        self.include = config.get('include', [])
        self.exclude = config.get('exclude', [])

        # Redirect stderr to error file
        sys.stderr = self.errorfile


    '''
    Set config directories and schema from just problemdir, datadir and outputdir
    '''
    def initialize_simple(self, problemdir, datadir, outputdir, include = [], exclude = []):
        self.initialize_from_config(
            self.create_simple_config(problemdir, datadir, outputdir, include, exclude)
        )

    '''
    Create config from problemdir, datadir, outputdir
    '''
    def create_simple_config(self, problemdir, datadir, outputdir, include = [], exclude = []):
        return {
            "problem_root": problemdir,
            "problem_schema": problemdir + os.sep + 'problemDoc.json',
            "training_data_root": datadir,
            "dataset_schema": datadir + os.sep + 'datasetDoc.json',
            'pipeline_logs_root': outputdir + os.sep + "logs",
            'executables_root': outputdir + os.sep + "executables",
            'temp_storage_root': outputdir + os.sep + "temp",
            "timeout": 60,
            "cpus"  : "4",
            "ram"   : "4Gi",
            "include": include,
            "exclude": exclude
            #"max_ensemble" : 5
            }


    """
    Set the task type, metric and output type via the schema
    """
    def load_problem(self):
        problemroot = self._dir(self.config, 'problem_root')
        problemdoc = self.config.get('problem_schema', None)
        assert(problemroot is not None)
        self.problem.load_problem(problemroot, problemdoc)

    """
    Initialize data from the config
    """
    def initialize_training_data_from_config(self):
        dataroot = self._dir(self.config, 'training_data_root')
        datadoc = self.config.get('dataset_schema', None)
        assert(dataroot is not None)
        dataset = Dataset()
        dataset.load_dataset(dataroot, datadoc)
        self.data_manager.initialize_data(self.problem, [dataset], view='TRAIN')

    """
    Initialize from features

    - Used by TA3
    """
    def initialize_from_features_simple(self, datafile, train_features, target_features, outputdir, view=None):
        data_directory = os.path.dirname(datafile)
        config = self.create_simple_config(outputdir, data_directory, outputdir)
        self.initialize_from_features(datafile, train_features, target_features, config, view)

    """

    Initialize all from features and config
    - Used by TA3
    """

    def initialize_from_features(self, datafile, train_features, target_features, config, view=None):
        self.initialize_from_config(config)
        data_directory = os.path.dirname(datafile)

        # Load datasets first
        filters = {}
        targets = {}
        dataset = Dataset()
        dataset.load_dataset(data_directory, datafile)

        if train_features is not None:
            filters[dataset.dsID] = list(map(
                lambda x: {"resID": x.resource_id, "colName": x.feature_name}, train_features
            ))
            self.problem.dataset_filters = filters

        if target_features is not None:
            targets[dataset.dsID] = list(map(
                lambda x: {"resID": x.resource_id, "colName": x.feature_name}, target_features
            ))
            self.problem.dataset_targets = targets

        self.data_manager.initialize_data(self.problem, [dataset], view)

    def get_pipeline_sorter(self):
        if self._pipeline_sorter is None:
            self._pipeline_sorter = OneStandardErrorPipelineSorter(self.problem.metrics[0])
        return self._pipeline_sorter


    """
    Initialize the L1 and L2 planners
    """
    def initialize_planners(self):
        self.l1_planner = LevelOnePlannerProxy(self.libdir, self.execution_helper, include = self.include, exclude = self.exclude)
        self.l2_planner = LevelTwoPlanner(self.libdir, self.execution_helper)


    """
    Train and select pipelines
    """

    def train(self, planner_event_handler, cutoff=10, ensemble=True, timeout=300, ensemble_median = True):
        """Train and select pipelines"""
        self.pe = planner_event_handler

        if ensemble:
            self.ensemble = Ensemble(self.problem, median = ensemble_median) #,self.max_ensemble)

        # Get data details
        df = copy.copy(self.data_manager.input_data)
        df_lbl = copy.copy(self.data_manager.target_data)
        df_profile = DataProfile(df)
        self.logfile.write("Data profile: %s\n" % df_profile)

        # Generate pipelines and store in self.exec_pipelines
        print('I am here')

        # l2_pipelines = self.generate_executable_pipelines(df, df_profile)


        print('generate_executable_pipelines')
        l1_pipelines = self.l1_planner.get_pipelines(df)
        if l1_pipelines is None:
            # If no L1 Pipelines, then we don't support this problem
            yield self.pe.ProblemNotImplemented()
            return []

        print('l1_pipelines: {}'.format([p for p in l1_pipelines]))
        l2_pipelines = []
        for l1_pipeline in l1_pipelines:
            if self.cache.l1_pipelines_handled.get(str(l1_pipeline), False):
                continue

            l2_pipeline_list = self.l2_planner.expand_pipeline(l1_pipeline, df_profile)
            self.cache.l1_pipelines_handled[str(l1_pipeline)] = True

            print('l2_pipeline_list: {}'.format([p for p in l2_pipeline_list]))

            if l2_pipeline_list:
                for l2_pipeline in l2_pipeline_list:
                    if not self.cache.l2_pipelines_handled.get(str(l2_pipeline), False):
                        self.cache.l2_l1_map[l2_pipeline.id] = l1_pipeline
                        l2_pipelines.append(l2_pipeline)
                        self.cache.l2_pipelines_map[str(l2_pipeline)] = l2_pipeline
                        yield self.pe.SubmittedPipeline(l2_pipeline)
        print('l2_pipelines: {}'.format([p for p in l2_pipelines]))

        if not l2_pipelines:
            return

        # Add feature selection
        all_extended_pipelines = []
        for l2_pipeline in l2_pipelines:
            extended_pipelines = self.l1_planner.extend_pipeline_with_feature_selection(l2_pipeline)
            all_extended_pipelines = all_extended_pipelines + extended_pipelines

        l2_pipelines = all_extended_pipelines


        for l2_pipeline in l2_pipelines:
            yield self.pe.RunningPipeline(l2_pipeline)

        callbacks = []
        for pipeline in l2_pipelines:
            # func = functools.partial(test_pipeline_result_call_back, pipeline, df, df_lbl)
            # test_result = func('test')
            callbacks.append(functools.partial(self.pipeline_result_call_back, pipeline, df, df_lbl))

        self.resource_manager.execute_pipelines(
            l2_pipelines, df, df_lbl, callbacks, timeout=timeout)

        self.exec_pipelines = self.resource_manager.exec_pipelines
        # self.exec_pipelines = sorted(self.exec_pipelines, key=lambda x: self._sort_by_metric(x))
        self.exec_pipelines = self.get_pipeline_sorter().sort_pipelines(self.exec_pipelines)


        # Create ensemble
        if ensemble:
            try:
                ensemble_pipeline = self.ensemble.greedy_add(
                    self.exec_pipelines, df, df_lbl, cv = self.resource_manager.cross_validation_folds, seed = self.resource_manager.cv_seed)
                if ensemble_pipeline:
                    self.exec_pipelines.append(ensemble_pipeline)

                    # Add to ensemble pipeline to stats
                    self.resource_manager.stats.pipeline_pending(ensemble_pipeline)
                    self.resource_manager.stats.pipeline_running(ensemble_pipeline)
                    self.resource_manager.stats.pipeline_finished(ensemble_pipeline)
            except Exception as e:
                traceback.print_exc()
                sys.stderr.write("ERROR ensemble.greedy_add : %s\n" % e)

        self.resource_manager.stats.print_successful_pipelines()
        self.write_training_results()
        print('DONE controller.train()')

        #print('running tests')
        #self.test_pipelines()
        #self.write_test_results()



    def pipeline_result_call_back(self, pipeline, df, df_lbl, task: asyncio.Future):
        if self.hyperparam_count > NUMBER_HYPERPARAM_SEARCHES:
            print('call_back limit reached')
            return

        self.hyperparam_count += 1
        exec_pipeline = task.result()
        if exec_pipeline is None:
            print('call_back pipeline failed %s' % pipeline.id)
        else:
            new_pipelines = self.tuner.generate_new_pipelines(exec_pipeline)
            # Problem with yields in this function.
            # for l2_pipeline in new_pipelines:
            #     yield self.pe.SubmittedPipeline(l2_pipeline)
            # for l2_pipeline in new_pipelines:
            #     yield self.pe.RunningPipeline(l2_pipeline)
            for l2_pipeline in new_pipelines:
                self.resource_manager.add_pipeline(l2_pipeline, df, df_lbl,
                                                   functools.partial(self.pipeline_result_call_back, pipeline, df, df_lbl))


    '''
    Write training results to file
    '''
    def write_training_results(self):
        # Sort pipelines

        self.exec_pipelines = self.get_pipeline_sorter().sort_pipelines(self.exec_pipelines)

        # Ended planners
        self._show_status("Found total %d successfully executing pipeline(s)..." % len(self.exec_pipelines))

        # Create executables
        self.pipelinesfile.write("# Pipelines ranked by (adjusted) metrics (%s)\n" % self.problem.metrics)
        for index in range(0, len(self.exec_pipelines)):
            pipeline = self.exec_pipelines[index]
            rank = index + 1
            # Format the metric values
            metric_values = []
            for metric in pipeline.planner_result.metric_values.keys():
                metric_value = pipeline.planner_result.metric_values[metric]
                metric_values.append("%s = %2.4f" % (metric, metric_value))

            self.pipelinesfile.write("%s ( %s ) : %s\n" % (pipeline.id, pipeline, metric_values))
            #self.pipelinesfile.write("\n Failed Pipelines \n")
            #self.pipelinesfile.write("%s\n" % str(self.failed_pipelines))
            self.execution_helper.create_pipeline_executable(pipeline, self.config)
            self.create_pipeline_logfile(pipeline, rank)

        # Flush pipeline
        self.pipelinesfile.flush()

        # save statistics
        with open(self.statistics_filename, 'w') as outfile:
            self.resource_manager.stats.json_line_dump(outfile, problem_id=self.problem.get_problem_id(),
                                                       dataset_names=self.problem.get_dataset_ids())
    def write_test_results(self):
        # Sort pipelines
        # self.exec_pipelines = sorted(self.exec_pipelines, key=lambda x: self._sort_by_metric(x))


        #self.exec_pipelines = self.get_pipeline_sorter().sort_pipelines(self.exec_pipelines)
        # Ended planners
        self._show_status("Testing %d successfully executing pipeline(s)..." % len(self.exec_pipelines))

        # Create executables
        self.test_pipelinesfile.write("# Pipelines ranked by (adjusted) metrics (%s)\n" % self.problem.metrics)
        for index in range(0, len(self.exec_pipelines)):
            pipeline = self.exec_pipelines[index]
            rank = index + 1
            # Format the metric values
            metric_values = []
            for metric in pipeline.test_result.metric_values.keys():
                metric_value = pipeline.test_result.metric_values[metric]
                metric_values.append("%s = %2.4f" % (metric, metric_value))

            self.test_pipelinesfile.write("%s ( %s ) : %s\n" % (pipeline.id, pipeline, metric_values))
            #self.execution_helper.create_pipeline_executable(pipeline, self.config)
            self.create_pipeline_logfile(pipeline, rank)


    def test_pipelines(self):
        #handler = GRPC_PlannerEventHandler()
        for pipeline in self.exec_pipelines:
            #try:
            #    res = self.test(pipeline, None)
            #except Exception as e:
            #    print(e)
            for result in self.test(pipeline):#, handler):
                if result is not None:
                    yield result

    '''
    Predict results on test data given a pipeline
    '''
    def test(self, pipeline, test_event_handler = None):
        helper = ExecutionHelper(self.problem, self.data_manager)
        testdf = pd.DataFrame(copy.copy(self.data_manager.input_data))
        target_col = self.data_manager.target_columns[0]['colName']
        sys.stdout.flush()
        print("** Evaluating pipeline %s" % str(pipeline))
        sys.stdout.flush()

        metric_dict = defaultdict(int)
        for i in self.problem.metrics:
            metric_dict[i.name]= 0.0
        results = []
        pipelines = []

        if pipeline.ensemble is not None:
            try:
                for ens_pipeline in pipeline.ensemble.pipelines:
                    pipelines.append(ens_pipeline)
            except:
                pipelines.append(pipeline)
        else:
            pipelines.append(pipeline)
        for pipeline_ in pipelines:
            for primitive in pipeline_.primitives:
                # Initialize primitive
                try:
                    print("Executing %s" % primitive)
                    sys.stdout.flush()
                    if primitive.task == "Modeling":
                        if primitive.unified_interface:
                            result = pd.DataFrame(primitive.executables.produce(inputs=testdf).value, index=testdf.index, columns=[target_col])
                        else:
                            result = pd.DataFrame(primitive.executables.predict(testdf), index=testdf.index, columns=[target_col])
                        for ind in range(len(self.problem.metrics)):
                            metric = self.problem.metrics[ind]
                            metric_fn = self.problem.metric_functions[ind]
                            metric_val = self.execution_helper._call_function(metric_fn, self.data_manager.target_data, result)
                            metric_dict[metric.name] = metric_dict[metric.name]+ metric_val
                        results.append(result)
                        #pipeline.test_result = PipelineExecutionResult(result, metric_dict)
                        break
                    elif primitive.task == "PreProcessing":
                        testdf = helper.test_execute_primitive(primitive, testdf)
                    elif primitive.task == "FeatureExtraction":
                        testdf = helper.test_featurise(primitive, testdf)
                    if testdf is None:
                        break
                except Exception as e:
                    sys.stderr.write(
                        "ERROR test(%s) : %s\n" % (pipeline_, e))
                    traceback.print_exc()
        print('aggregating results')
        for k, v in metric_dict.items():
            metric_dict[k] = v/len(pipelines)
        #try:
        #    method = pipeline.ensemble.method
        #except:
        #    method = 'mean'
        method = 'mean'
        if method == 'mean':
            res = numpy.mean(numpy.array(results), axis = 0)
        elif method == 'median':
            res  = numpy.median(numpy.array(results), axis = 0)

        pipeline.test_result = PipelineExecutionResult(pd.DataFrame(res), metric_dict, None)

        if test_event_handler is not None:
            yield test_event_handler.ExecutedPipeline(pipeline)


    def stop(self):
        '''
        Stop planning, and write out the current list (sorted by metric)
        '''

    def create_pipeline_logfile(self, pipeline, rank):
        logfilename = "%s%s%s.json" % (self.log_dir, os.sep, pipeline.id)
        logdata = {
            "problem_id": self.problem.prID,
            "pipeline_rank": rank,
            "name": pipeline.id,
            "primitives": []
        }
        primitive_set = set()
        ensembling = pipeline.ensemble is not None
        if ensembling:
            for epipe in pipeline.ensemble.pipelines:
                for primitive in epipe.primitives:
                    primitive_set.add(primitive.cls)
        else:
            for primitive in pipeline.primitives:
                primitive_set.add(primitive.cls)

        logdata["primitives"] = list(primitive_set)
        with(open(logfilename, 'w')) as pipelog:
            json.dump(logdata, pipelog,
                sort_keys=True, indent=4, separators=(',', ': '))
            pipelog.close()

    def _dir(self, config, key, makeflag=False):
        dir = config.get(key)
        if dir is None:
            return None
        dir = os.path.abspath(dir)
        if makeflag and not os.path.exists(dir):
            os.makedirs(dir)
        return dir

    def _show_status(self, status):
        print(status)
        sys.stdout.flush()

    def _sort_by_metric(self, pipeline):
        # NOTE: Sorting/Ranking by first metric only
        if self.problem.metrics[0].larger_is_better():
            return -pipeline.planner_result.metric_values[self.problem.metrics[0].name]
        return pipeline.planner_result.metric_values[self.problem.metrics[0].name]
