""" Interface class for the LISP-based mReasoner implementation.

"""

import copy
import logging
import os
import platform
import queue
import subprocess
import threading
import time

import urllib.request
import zipfile

import numpy as np
import scipy.optimize as so


FASL_ENDINGS = {
    'Darwin': 'dx64fsl',
    'Windows': 'wx64fsl',
    'Linux': 'lx64fsl'
}

def source_path(mreas_path='.mreasoner'):
    """ Determines the source path of mReasoner if existent. Downloads a copy if necessary.

    Parameters
    ----------
    mreas_path : str
        Target path for the mReasoner source copy.

    Returns
    -------
    str
        Path to the directory containing the mReasoner sources.

    """

    if not os.path.exists(mreas_path):
        # Create the mreasoner directory
        os.mkdir(mreas_path)

        # Download the mreasoner source
        link = 'https://nc.informatik.uni-freiburg.de/index.php/s/JyMd3g36wXdgwy3/download'
        dl_target = mreas_path + os.sep + 'mReasoner.zip'
        urllib.request.urlretrieve(link, dl_target)

        # Unzip content
        with zipfile.ZipFile(dl_target, 'r') as zip_ref:
            zip_ref.extractall(mreas_path)

    # Look for mReasoner directory
    for name in os.listdir(mreas_path):
        path = mreas_path + os.sep + name
        if not os.path.isdir(path) or name.startswith('_'):
            continue
        mreas_path = path + os.sep + 'src'

    return mreas_path

class MReasoner():
    """ LISP mReasoner wrapper. Executes a Clozure Common LISP subprocess to run an unmodified
    version of mReasoner. Provides basic interfacing mechanisms for inference generation and
    parameter fitting.

    """

    def __init__(self, ccl_path, mreasoner_dir):
        """ Constructs the mReasoner instance by launching the LISP subprocess.

        Parameters
        ----------
        ccl_path : str
            Path to the Clozure Common LISP executable.

        mreasoner_dir : str
            Path to the mReasoner source code directory.

        """

        # Initialize logger instance
        self.logger = logging.getLogger(__name__)
        self.param_bounds = [[0.0, 1.0], [0.1, 8.0], [0.0, 1.0], [0.0, 1.0]]

        self.proc = subprocess.Popen(
            [ccl_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )

        # Add debug stuff
        self.received_messages = []
        self.executed_commands = []

        # Instantiate the result queue
        self.resp_queue = queue.Queue()

        # Register the readers
        def stdout_reader(proc):
            # Setup thread logger
            logger = logging.getLogger(__name__ + '-reader')
            logger.debug('Starting reader...')

            while True:
                # Read text from mReasoner output
                text = proc.stdout.readline().decode('ascii').strip()
                logger.debug('mReasoner:%s', text)
                self.received_messages.append(text)

                # Ignore comments and query results
                if text.startswith(';'):
                    continue

                # Catch the termination signal
                if 'TERMINATE' in text:
                    logger.debug('termination handling initiated...')
                    break
                if ('While executing:' in text) or ('Error:' in text):
                    self.resp_queue.put('HALT')

                # Handle query results
                if text.startswith('?'):
                    query_result = text[2:]

                    # Handle different query results
                    is_float = False
                    try:
                        float(query_result)
                        is_float = True
                    except ValueError:
                        pass

                    # Ignore float results
                    if is_float:
                        continue

                    if query_result[0] == '(' and query_result[-1] == ')':
                        logger.debug('Queue-Quant:%s', query_result)
                        self.resp_queue.put(query_result)
                    elif query_result[0] == '"' and query_result[-1] == '"':
                        query_result = query_result.replace('"', '')
                        logger.debug('Queue-Concl:%s', query_result)
                        self.resp_queue.put(query_result)
                    elif query_result == 'NIL':
                        logger.debug('Queue-NIL:%s', query_result)
                        self.resp_queue.put(query_result)
                    else:
                        logger.debug('Queue-INVAL:%s', query_result)

            logger.debug('terminating...')

        self.readerstdout = threading.Thread(target=stdout_reader, args=(self.proc,), daemon=True)
        self.readerstdout.start()

        # Load mReasoner in CCL environment
        mreasoner_file = mreasoner_dir + os.sep + "+mReasoner.lisp"
        fasl_path = mreasoner_dir + os.sep + "+mReasoner.{}".format(FASL_ENDINGS[platform.system()])

        # Compile mreasoner if not done before
        if not os.path.exists(fasl_path):
            mreasoner_file = mreasoner_file.replace('\\', '/')
            self._send('(compile-file "{}")'.format(mreasoner_file))

        fasl_path = fasl_path.replace('\\', '/')
        self._send('(load "{}")'.format(fasl_path))
        self._send('(defvar resp 0)')

        # Initialize parameter values
        self.default_params = {
            'epsilon': 0.0,
            'lambda': 4.0,
            'omega': 1.0,
            'sigma': 0.0
        }
        self.params = copy.deepcopy(self.default_params)

        self.set_param('epsilon', self.params['epsilon'])
        self.set_param('lambda', self.params['lambda'])
        self.set_param('omega', self.params['omega'])
        self.set_param('sigma', self.params['sigma'])

    def _send(self, cmd):
        """ Send a command to the Clozure Common LISP subprocess.

        Parameters
        ----------
        cmd : str
            Command to send.

        """

        # Normalize the command
        cmd.strip()
        self.executed_commands.append(cmd)

        self.logger.debug('Send:%s', cmd)
        self.proc.stdin.write('{}\n'.format(cmd).encode('ascii'))
        self.proc.stdin.flush()

    @staticmethod
    def construct_premises(syllog):
        """ Constructs mReasoner representation of the premises for a given syllogism identifier.

        Parameters
        ----------
        syllog : str
            Syllogistic problem identifier (e.g., 'AA1', 'OE3').

        Returns
        -------
        (p1, p2) : str
            Tuple of the mReasoner representations of both premises.

        """

        template_quant = {
            'A': 'All {} are {}',
            'I': 'Some {} are {}',
            'E': 'No {} are {}',
            'O': 'Some {} are not {}'
        }

        template_fig = {
            '1': [['A', 'B'], ['B', 'C']],
            '2': [['B', 'A'], ['C', 'B']],
            '3': [['A', 'B'], ['C', 'B']],
            '4': [['B', 'A'], ['B', 'C']]
        }

        prem1 = template_quant[syllog[0]].format(*template_fig[syllog[-1]][0])
        prem2 = template_quant[syllog[1]].format(*template_fig[syllog[-1]][1])
        return prem1, prem2

    def query(self, syllog):
        """ Queries mReasoner for a prediction for a given syllogistic problem.

        Parameters
        ----------
        syllog : str
            Syllogistic problem identifier (e.g., 'AA1', 'EO3') to generate prediction for.

        Returns
        -------
        str
            Generated syllogistic response identifier (e.g., 'Aac', 'Ica')

        """

        prem1, prem2 = self.construct_premises(syllog)

        # Send the conclusion generation query
        cmd = "(what-follows? (list (parse '({})) (parse '({}))))".format(prem1, prem2)
        cmd = '(setf resp {})'.format(cmd)
        self.logger.debug('Query:%s', cmd)
        self._send(cmd)

        # Check query result
        query_result = self.resp_queue.get()
        self.logger.debug('Query result: "%s"', query_result)

        conclusion = None
        if 'Q-INTENSION' in query_result:
            # Send the result interpretation query
            # cmd = '(abbreviate (nth (random (length resp)) resp))'
            cmd = "(map 'list (lambda (x) (abbreviate x)) resp)"
            self.logger.debug('Query:%s', cmd)
            self._send(cmd)

            # Retrieve queue output
            conclusion = self.resp_queue.get()
        elif ('NULL-INTENSION' in query_result) or (query_result == 'NIL'):
            self.logger.debug('NVC-RESULT:%s', query_result)
            if 'Q-INTENSION' in query_result:
                assert False
            conclusion = 'NVC'
        else:
            self.logger.warning('QUERY-RES-INVALID:%s', query_result)
            assert False

        conclusion_candidates = conclusion.replace('"', '').replace('(', '').replace(')', '').split()
        self.logger.debug('%s->%s', syllog, conclusion_candidates)
        return conclusion_candidates

    def terminate(self):
        """ Terminate mReasoner and its parent instance of Clozure Common LISP.

        """

        # Shutdown the threads
        self._send('(prin1 "TERMINATE")')
        self.logger.debug('Waiting for stdout...')
        self.readerstdout.join()

        # Terminate Clozure
        self._send('(quit)')

    def set_param(self, param, value):
        """ Set mReasoner parameter to a specified value.

        Parameter
        ---------
        param : str
            Parameter identifier. Can be one of ['epsilon', 'lambda', 'omega', 'sigma'].

        value : float
            Parameter value.

        Raises
        ------
        ValueError
            If invalid param is specified.

        """

        if param not in self.params:
            raise ValueError('Attempted to set invalid parameter: {}'.format(param))
        self.params[param] = value

        # Send parameter change to mReasoner
        cmd = '(setf +{}+ {:f})'.format(param, value)
        self.logger.debug('Param-Set: %s->%f:%s', param, value, cmd)
        self._send(cmd)

    def set_param_vec(self, params):
        """ Directly set a vector of params.

        Parameters
        ----------
        params : list(float)
            Vector of parameter values. Interpreted according to the order ['epsilon', 'lambda',
            'omega', 'sigma'].

        """

        param_names = ['epsilon', 'lambda', 'omega', 'sigma']
        for name, value in zip(param_names, params):
            self.set_param(name, value)

    def _fit_fun(self, params, train_x, train_y, include_param=False):
        """ Fitting helper function. Receives parameter values and computes accuracy on given
        training and test data.

        Parameters
        ----------
        params : list(float)
            List of parameters.

        include_params : boolean
            Flag to indicate that params are to be included in results.

        Results
        -------
        float
            Predictive accuracy.

        list(float), optional
            Parameters

        """

        # Set the parameters
        self.set_param_vec(params)

        score = 0
        for task, resp in zip(train_x, train_y):
            pred = self.query(task)
            score += 1/len(pred) if resp in pred else 0

        inaccuracy = 1 - (score / len(train_x))
        self.logger.debug('Fitting-Eval: (p=%s): %f', params, inaccuracy)

        if include_param:
            return inaccuracy, params

        return inaccuracy

    def fit(self, train_x, train_y, num_fits=10):
        """ Fits mReasoner parameters to the specified data.

        Parameters
        ----------
        train_x : list(str)
            List of syllogistic task encodings (e.g., 'AA1').

        train_y : list(str)
            List of syllogistic response encodings (e.g., 'Aac').

        Returns
        -------
        float
            Fit result accuracy.

        """

        results = []
        for idx in range(num_fits):
            self.logger.debug('Starting fit %d/%d...', idx + 1, num_fits)
            start_time = time.time()

            # start_params = [x[1] for x in sorted(self.params.items())]
            start_params = [np.random.uniform(lims[0], lims[1]) for lims in self.param_bounds]

            res = so.minimize(
                self._fit_fun,
                start_params,
                method='L-BFGS-B',
                bounds=self.param_bounds,
                args=(train_x, train_y))

            if res.success:
                self.logger.debug('Fitting iteration success:\n%s', res)
                results.append((res.fun, res.x))
            else:
                self.logger.warning('Fitting iteration failed:\n%s', res)

            self.logger.debug('...fit took {:4f}s'.format(time.time() - start_time))

        if len(results) != num_fits:
            self.logger.warning(
                '%d/%d fitting runs unsuccessful', num_fits - len(results), num_fits)
            # If all fits unsuccessful, use default parameters
            if not results:
                self.logger.warning('Fitting failed, setting to default params')
                for param, value in self.default_params.items():
                    self.set_param(param, value)
                return -1, [self.default_params[x] for x in ['epsilon', 'lambda', 'omega', 'sigma']]

        # Obtain best parameter configuration
        optim_score, optim_params = sorted(results, key=lambda x: x[0])[0]
        self.set_param_vec(optim_params)

        return optim_score, optim_params

    def fit_grid(self, train_x, train_y, num=10):
        best_error = 2
        best_params = None

        for p_epsilon in np.linspace(*self.param_bounds[0], num):
            for p_lambda  in np.linspace(*self.param_bounds[1], num):
                for p_omega in np.linspace(*self.param_bounds[2], num):
                    for p_sigma in np.linspace(*self.param_bounds[3], num):
                        params = [p_epsilon, p_lambda, p_omega, p_sigma]
                        start = time.time()
                        error = self._fit_fun(params, train_x, train_y)
                        self.logger.debug('Grid fit iteration took {:.2f}s'.format(
                            time.time() - start))

                        if error < best_error:
                            best_error = error
                            best_params = params

        self.set_param_vec(best_params)
        return best_error, best_params

    def fit_rnd(self, train_x, train_y, num=10, old_params=None):
        best_error = 2
        best_params = None

        if old_params:
            best_params = old_params
            best_error = self._fit_fun(best_params, train_x, train_y)

        for _ in range(num):
            p_epsilon = np.random.uniform(*self.param_bounds[0])
            p_lambda = np.random.uniform(*self.param_bounds[1])
            p_omega = np.random.uniform(*self.param_bounds[2])
            p_sigma = np.random.uniform(*self.param_bounds[3])

            params = [p_epsilon, p_lambda, p_omega, p_sigma]
            error = self._fit_fun(params, train_x, train_y)

            if error < best_error:
                best_error = error
                best_params = params

        self.set_param_vec(best_params)
        return best_error, best_params
