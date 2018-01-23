import collections
import datetime
import functools
import inspect
import math

import astromodels
import astropy.units as u
import numpy as np
import pandas as pd
from astromodels.core.model_parser import ModelParser
from astromodels.core.my_yaml import my_yaml
from astromodels.core.parameter import Parameter
from corner import corner

try:

    import chainconsumer

except:

    has_chainconsumer = False

else:

    has_chainconsumer = True

from threeML.exceptions.custom_exceptions import custom_warnings
from threeML.io.file_utils import sanitize_filename
from threeML.io.fits_file import fits, FITSFile, FITSExtension
from threeML.io.rich_display import display
from threeML.io.table import NumericMatrix
from threeML.io.uncertainty_formatter import uncertainty_formatter
from threeML.io.results_table import ResultsTable
from threeML.version import __version__
from threeML.random_variates import RandomVariates
from threeML.io.calculate_flux import _calculate_point_source_flux
from threeML.config.config import threeML_config

# These are special characters which cannot be safely saved in the keyword of a FITS file. We substitute
# them with normal characters when we write the keyword, and we substitute them back when we read it back
_subs = (('\n', "_NEWLINE_"), ("'", "_QUOTE1_"), ('"', "_QUOTE2_"), ('{', "_PARO_"), ('}', "_PARC_"))


def _escape_yaml_for_fits(yaml_code):
    for sub in _subs:
        yaml_code = yaml_code.replace(sub[0], sub[1])

    return yaml_code


def _escape_back_yaml_from_fits(yaml_code):
    for sub in _subs:
        yaml_code = yaml_code.replace(sub[1], sub[0])

    return yaml_code


def load_analysis_results(fits_file):
    """
    Load the results of one or more analysis from a FITS file produced by 3ML

    :param fits_file: path to the FITS file containing the results, as output by MLEResults or BayesianResults
    :return: a new instance of either MLEResults or Bayesian results dending on the type of the input FITS file
    """

    with fits.open(fits_file) as f:

        n_results = map(lambda x: x.name, f).count('ANALYSIS_RESULTS')

        if n_results == 1:

            return _load_one_results(f['ANALYSIS_RESULTS', 1])

        else:

            return _load_set_of_results(f, n_results)


def _load_one_results(fits_extension):
    # Gather analysis type
    analysis_type = fits_extension.header.get("RESUTYPE")

    # Gather the optimized model
    serialized_model = _escape_back_yaml_from_fits(fits_extension.header.get("MODEL"))
    model_dict = my_yaml.load(serialized_model)

    optimized_model = ModelParser(model_dict=model_dict).get_model()

    # Gather statistics values
    statistic_values = collections.OrderedDict()

    measure_values = collections.OrderedDict()

    for key in fits_extension.header.keys():

        if key.find("STAT") == 0:
            # Found a keyword with a statistic for a plugin
            # Gather info about it

            id = int(key.replace("STAT", ""))
            value = float(fits_extension.header.get(key))
            name = fits_extension.header.get("PN%i" % id)
            statistic_values[name] = value

        if key.find("MEAS") == 0:
            # Found a keyword with a statistic for a plugin
            # Gather info about it

            id = int(key.replace("MEAS", ""))
            name = fits_extension.header.get(key)
            value = float(fits_extension.header.get("MV%i" % id))
            measure_values[name] = value

    if analysis_type == "MLE":

        # Get covariance matrix

        covariance_matrix = fits_extension.data.field("COVARIANCE").T

        # Instance and return

        return MLEResults(optimized_model, covariance_matrix, statistic_values, statistical_measures=measure_values)

    elif analysis_type == "Bayesian":

        # Gather samples
        samples = fits_extension.data.field("SAMPLES")

        # Instance and return

        return BayesianResults(optimized_model, samples.T, statistic_values, statistical_measures=measure_values)


def _load_set_of_results(open_fits_file, n_results):
    # Gather all results
    all_results = []

    for i in range(n_results):
        all_results.append(_load_one_results(open_fits_file['ANALYSIS_RESULTS', i + 1]))

    this_set = AnalysisResultsSet(all_results)

    # Now gather the SEQUENCE extension and set the characterization frame accordingly

    sequence_ext = open_fits_file['SEQUENCE']

    seq_type = sequence_ext.header.get("SEQ_TYPE")

    # Build the data tuple
    record = sequence_ext.data

    data_list = []

    for column in record.columns:

        if column.unit is None:

            this_tuple = (column.name, record[column.name])

        else:

            this_tuple = (column.name, record[column.name] * u.Unit(column.unit))

        data_list.append(this_tuple)

    this_set.characterize_sequence(seq_type, tuple(data_list))

    return this_set


class SEQUENCE(FITSExtension):
    """
    Represents the SEQUENCE extension of a FITS file containing a set of results from a set of analysis

    """

    _HEADER_KEYWORDS = [
        ('EXTNAME', 'SEQUENCE', 'Extension name'),
        ('ORIGIN', '3ML', 'Multi-Mission Max. Likelihood v. %s' % __version__),
        ('SEQ_TYPE', None, 'Description of sequence type')
    ]

    def __init__(self, name, data_tuple):
        # Init FITS extension

        super(SEQUENCE, self).__init__(data_tuple, self._HEADER_KEYWORDS)

        # Update keywords
        self.hdu.header.set("SEQ_TYPE", name)


class ANALYSIS_RESULTS(FITSExtension):
    """
    Represents the ANALYSIS_RESULTS extension of a FITS file encoding the results of an analysis

    :param analysis_results:
    :type analysis_results: _AnalysisResults
    """

    _HEADER_KEYWORDS = [
        ('EXTNAME', 'ANALYSIS_RESULTS', 'Extension name'),
        ('MODEL', None, 'A pseudo-yaml serialization of the model'),
        ('ORIGIN', '3ML', 'Multi-Mission Max. Likelihood v. %s' % __version__),
        ('RESUTYPE', None, 'Analysis producing results (MLE or Bayesian)')
    ]

    def __init__(self, analysis_results):

        optimized_model = analysis_results.optimized_model

        # Gather the dictionary with free parameters

        free_parameters = optimized_model.free_parameters

        n_parameters = len(free_parameters)

        # Gather covariance matrix (if any)

        if analysis_results.analysis_type == "MLE":

            assert isinstance(analysis_results, MLEResults)

            covariance_matrix = analysis_results.covariance_matrix

            # Check that the covariance matrix has the right shape

            assert covariance_matrix.shape == (n_parameters, n_parameters), \
                "Matrix has the wrong shape. Should be %i x %i, got %i x %i" % (n_parameters, n_parameters,
                                                                                covariance_matrix.shape[0],
                                                                                covariance_matrix.shape[1])

            # Empty samples set
            samples = np.zeros(n_parameters)

        else:

            assert isinstance(analysis_results, BayesianResults)

            # Empty covariance matrix

            covariance_matrix = np.zeros(n_parameters)

            # Gather the samples
            samples = analysis_results._samples_transposed

        # Serialize the model so it can be placed in the header

        yaml_model_serialization = my_yaml.dump(optimized_model.to_dict_with_types())

        # Replace characters which cannot be contained in a FITS header with other characters
        yaml_model_serialization = _escape_yaml_for_fits(yaml_model_serialization)

        # Get data frame with parameters (always use equal tail errors)

        data_frame = analysis_results.get_data_frame(error_type="equal tail")

        # Prepare columns

        data_tuple = [('NAME', free_parameters.keys()),
                      ('VALUE', data_frame['value'].values),
                      ('NEGATIVE_ERROR', data_frame['negative_error'].values),
                      ('POSITIVE_ERROR', data_frame['positive_error'].values),
                      ('ERROR', data_frame['error'].values),
                      ('UNIT', np.array(data_frame['unit'].values, str)),
                      ('COVARIANCE', covariance_matrix),
                      ('SAMPLES', samples)]

        # Init FITS extension

        super(ANALYSIS_RESULTS, self).__init__(data_tuple, self._HEADER_KEYWORDS)

        # Update keywords with their values for this instance
        self.hdu.header.set("MODEL", yaml_model_serialization)
        self.hdu.header.set("RESUTYPE", analysis_results.analysis_type)

        # Now add two keywords for each instrument
        stat_series = analysis_results.optimal_statistic_values  # type: pd.Series

        for i, (plugin_instance_name, stat_value) in enumerate(stat_series.iteritems()):
            self.hdu.header.set("STAT%i" % i, stat_value, comment="Stat. value for plugin %i" % i)
            self.hdu.header.set("PN%i" % i, plugin_instance_name, comment="Name of plugin %i" % i)

        # Now add the statistical measures

        measure_series = analysis_results.statistical_measures # type: pd.Series

        for i, (measure, measure_value) in enumerate(measure_series.iteritems()):
            self.hdu.header.set("MEAS%i" % i, measure, comment="Measure type %i" % i)
            self.hdu.header.set("MV%i" % i, measure_value, comment="Measure value %i" % i)



class AnalysisResultsFITS(FITSFile):
    """
    A FITS file for storing one or more results from 3ML analysis

    """

    def __init__(self, *analysis_results, **kwargs):

        # This will contain the list of extensions we want to write in the file

        extensions = []

        if 'sequence_name' in kwargs:
            # This is a set of results

            assert 'sequence_tuple' in kwargs

            # We got elements to write the SEQUENCE extension

            # Make SEQUENCE extension
            sequence_ext = SEQUENCE(kwargs['sequence_name'], kwargs['sequence_tuple'])

            extensions.append(sequence_ext)

        # Make one extension for each analysis results

        results_ext = map(ANALYSIS_RESULTS, analysis_results)

        # Fix the EXTVER keyword (must be increasing among extensions with same name
        for i, res_ext in enumerate(results_ext):
            res_ext.hdu.header.set("EXTVER", i + 1)

        extensions.extend(results_ext)

        # Create FITS file
        super(AnalysisResultsFITS, self).__init__(fits_extensions=extensions)

        # Set a couple of keywords in the primary header
        self._hdu_list[0].header.set("DATE", datetime.datetime.now().isoformat())
        self._hdu_list[0].header.set("ORIGIN", "3ML", comment=('Multi-Mission Max. Likelihood v. %s' % __version__))


class _AnalysisResults(object):
    """
    A unified class to store results from a maximum likelihood or a Bayesian analysis, which provides a unique interface
    and allows for "error propagation" (which means different things in the two contexts) in arbitrary expressions.

    This class is not intended for public consumption. Use either the MLEResults or the BayesianResults subclasses.

    :param optimized_model: a Model instance with the optimized values of the parameters. A clone will be stored within
    the class, so there is no need to clone it before hand
    :type optimized_model: astromodels.Model
    :param samples: the samples for the parameters
    :type samples: np.ndarray
    :param statistic_values: a dictionary containing the statistic (likelihood or posterior) values for the different
    datasets
    :type statistic_values: dict
    """

    def __init__(self, optimized_model, samples, statistic_values, analysis_type, statistical_measures):

        # Safety checks

        self._n_free_parameters = len(optimized_model.free_parameters)

        assert samples.shape[1] == self._n_free_parameters, "Number of free parameters (%s) and set of samples (%s) " \
                                                            "do not agree." % (samples.shape[1],
                                                                               self._n_free_parameters)

        # NOTE: we clone the model so that whatever happens outside or after, this copy of the model will not be
        # changed

        self._optimized_model = astromodels.clone_model(optimized_model)

        # Save a transposed version of the samples for easier access

        self._samples_transposed = samples.T

        # Store likelihood values in a pandas Series

        self._optimal_statistic_values = pd.Series(statistic_values)

        # Store the statistical measures as a pandas Series

        self._statistical_measures = pd.Series(statistical_measures)

        # The .free_parameters property of the model is pretty costly because it needs to update all the parameters
        # to see if they are free. Since the saved model will not be touched we can cache that
        self._free_parameters = self._optimized_model.free_parameters

        # Gather also the optimized values of the parameters
        self._values = np.array(map(lambda x: x.value, self._free_parameters.values()))

        # Set the analysis type
        self._analysis_type = analysis_type

    @property
    def samples(self):
        """
        Returns the matrix of the samples

        :return:
        """

        return self._samples_transposed

    @property
    def analysis_type(self):

        return self._analysis_type

    def write_to(self, filename, overwrite=False):
        """
        Write results to a FITS file

        :param filename:
        :param overwrite:
        :return: None
        """

        fits_file = AnalysisResultsFITS(self)

        fits_file.writeto(sanitize_filename(filename), overwrite=overwrite)

    def get_variates(self, param_path):

        assert param_path in self._optimized_model.free_parameters, "Parameter %s is not a " \
                                                                    "free parameters of the model" % param_path

        param_index = self._free_parameters.keys().index(param_path)

        this_value = self._values[param_index]

        these_samples = self._samples_transposed[param_index]

        this_variate = RandomVariates(these_samples, value=this_value)

        return this_variate

    @staticmethod
    def propagate(function, **kwargs):
        """
        Allow for propagation of uncertainties on arbitrary functions. It returns a function which is a wrapper around
        the provided input function. Using the wrapper with RandomVariates instances as arguments will return a
        RandomVariates result, with the errors propagated.

        Example:

        def my_function(x, a, b, c):

            return a*x**2 + b*x + c

        > p1 = analysis_results.get_variates("src.spectrum.main.composite.a_1")
        > p2 = analysis_results.get_variates("src.spectrum.main.composite.b_1")
        > wrapped_function = analysis_results.propagate(my_function, a=p1, b=p2)
        > result = wrapped_function(x=1.0, c=2.3)
        > print(result)
        equal-tail: (4.24 -0.16 +0.15) x 10, hpd: (4.24 -0.05 +0.08) x 10

        NOTE: for simple operations, you do not need to use this. This will work:

        > res = p1 + p2
        > print(res)
        equal-tail: (4.11 -0.16 +0.15) x 10, hpd: (4.11 -0.05 +0.08) x 10

        :param function: function to be wrapped
        :param **kwargs: keyword arguments specifying which random variates should substitute which argument in the
        function (see example above)
        :return: a new function, wrapping function, which can be used to propagate errors
        """

        # Get calling sequence of input function
        # arguments will be a list of names, like ['a','b']
        arguments, _, _, _ = inspect.getargspec(function)

        # Get the arguments of function which have not been specified
        # in the calling sequence (the **kwargs dictionary)
        # (they will be excluded from the vectorization)
        to_be_excluded = [item for item in arguments if item not in kwargs.keys()]

        # Vectorize the function
        vectorized = np.vectorize(function, excluded=to_be_excluded)

        # Make a wrapper so we are sure that the arguments are used in the
        # right order, as they will be taken from the kwargs
        wrapper = functools.partial(vectorized, **kwargs)

        # Finally make so that the result is always a RandomVariate
        wrapper2 = lambda *args, **kwargs: RandomVariates(wrapper(*args, **kwargs))

        return wrapper2

    @property
    def optimized_model(self):
        """
        Returns a copy of the optimized model

        :return: a copy of the optimized model
        """

        return astromodels.clone_model(self._optimized_model)

    def estimate_covariance_matrix(self):
        """
        Estimate the covariance matrix from the samples

        :return: a covariance matrix estimated from the samples
        """

        return np.cov(self._samples_transposed)

    def get_correlation_matrix(self):

        raise NotImplementedError("You need to implement this")

    @property
    def optimal_statistic_values(self):

        return self._optimal_statistic_values

    @property
    def statistical_measures(self):

        return self._statistical_measures

    def _get_correlation_matrix(self, covariance):
        """
        Compute the correlation matrix

        :return: correlation matrix
        """

        # NOTE: we compute this on-the-fly because it is of less frequent use, and contains essentially the same
        # information of the covariance matrix.

        # Compute correlation matrix

        correlation_matrix = np.zeros_like(covariance)

        for i in range(self._n_free_parameters):

            variance_i = covariance[i, i]

            for j in range(self._n_free_parameters):

                variance_j = covariance[j, j]

                if variance_i * variance_j > 0:

                    correlation_matrix[i, j] = covariance[i, j] / (math.sqrt(variance_i * variance_j))

                else:

                    # This should not happen, but it might because a fit failed or the numerical differentiation
                    # failed

                    correlation_matrix[i, j] = np.nan

        return correlation_matrix

    def get_statistic_frame(self):

        raise NotImplementedError("You have to implement this")

    def _get_statistic_frame(self, name):

        logl_results = {}

        # Create a new ordered dict so we can add the total
        optimal_statistic_values = collections.OrderedDict(self._optimal_statistic_values.iteritems())

        # Add the total
        optimal_statistic_values['total'] = np.sum(self._optimal_statistic_values.values)

        logl_results[name] = optimal_statistic_values

        loglike_dataframe = pd.DataFrame(logl_results)

        return loglike_dataframe

    def get_statistic_measure_frame(self):
        """
        Returns a panadas DataFrame with additional statistical information including
        point and posterior based information criteria as well as their effective number
        of free parameters. To use these properly, it is vital you consult the statsitical
        literature.

        :return: a pandas DataFrame instance
        """

        return self._statistical_measures.to_frame(name='statistical measures')

    def _get_results_table(self, error_type, cl, covariance=None):

        if error_type == "equal tail":

            errors_gatherer = RandomVariates.equal_tail_interval

        elif error_type == "hpd":

            errors_gatherer = RandomVariates.highest_posterior_density_interval

        elif error_type == "covariance":

            assert covariance is not None, "If you use error_type='covariance' you have to provide a cov. matrix"

            errors_gatherer = None

        else:

            raise ValueError("error_type must be either 'equal tail' or 'hpd'. Got %s" % error_type)

        # Build the data frame
        parameter_paths = []
        values = []
        negative_errors = []
        positive_errors = []
        units_dict = []

        for i, this_par in enumerate(self._free_parameters.values()):

            parameter_paths.append(this_par.path)

            this_phys_q = self.get_variates(parameter_paths[-1])

            values.append(this_phys_q.value)

            units_dict.append(this_par.unit)

            if error_type != "covariance":

                low_bound, hi_bound = errors_gatherer(this_phys_q, cl)

                negative_errors.append(low_bound - values[-1])

                positive_errors.append(hi_bound - values[-1])

            else:

                std_dev = np.sqrt(covariance[i, i])

                if this_par.has_transformation():

                    best_fit_internal = this_par.transformation.forward(values[-1])

                    _, neg_error = this_par.internal_to_external_delta(best_fit_internal, -std_dev)
                    negative_errors.append(neg_error)

                    _, pos_error = this_par.internal_to_external_delta(best_fit_internal, std_dev)
                    positive_errors.append(pos_error)

                else:

                    negative_errors.append(-std_dev)
                    positive_errors.append(std_dev)

        results_table = ResultsTable(parameter_paths, values, negative_errors, positive_errors, units_dict)

        return results_table

    def get_data_frame(self, error_type="equal tail", cl=0.68):
        """
        Returns a pandas DataFrame with the parameters and their errors, computed as specified in "error_type" and
        with the confidence/credibility level specified in cl.

        Using "equal_tail" and cl=0.68 corresponds to the usual frequentist 1-sigma confidence interval

        :param error_type: "equal tail" or "hpd" (highest posterior density)
        :type error_type: str
        :param cl: confidence/credibility level (0 < cl < 1)
        :return: a pandas DataFrame instance
        """

        # Gather the errors

        return self._get_results_table(error_type, cl).frame

    def get_point_source_flux(self, ene_min, ene_max, sources=(), confidence_level=0.68,
                              flux_unit='erg/(s cm2)', use_components=False, components_to_use=(),
                              sum_sources=False):
        """

        :param ene_min: minimum energy (an astropy quantity, like 1.0 * u.keV. You can also use a frequency, like
        1 * u.Hz)
        :param ene_max: maximum energy (an astropy quantity, like 10 * u.keV. You can also use a frequency, like
        10 * u.Hz)
        :param sources: Use this to specify the name of the source or a tuple/list of source names to be plotted.
        If you don't use this, all sources will be plotted.
        :param confidence_level: the confidence level for the error (default: 0.68)
        :param flux_unit: (optional) astropy flux unit in string form (can be
        :param use_components: plot the components of each source (default: False)
        :param components_to_use: (optional) list of string names of the components to plot: including 'total'
        :param sum_sources: (optional) if True, also the sum of all sources will be plotted
        :return:
        """

        # Convert the ene_min and ene_max in pure numbers in keV
        _ene_min = ene_min.to("keV").value
        _ene_max = ene_max.to("keV").value

        _params = {
            'confidence_level': confidence_level,
            'equal_tailed': True,  # FIXME: what happens if this is False?
            'best_fit': 'median',
            'energy_unit': 'keV',
            'flux_unit': flux_unit,
            'use_components': use_components,
            'components_to_use': components_to_use,
            'sources_to_use': sources,
            'sum_sources': sum_sources,

        }

        mle_results, bayes_results = _calculate_point_source_flux(_ene_min, _ene_max, self, **_params)

        # The output contains one source per row
        def _format_error(row):

            rep = uncertainty_formatter(row['flux'].value, row['low bound'].value, row['hi bound'].value)

            # Represent the unit as a string
            unit_rep = str(row['flux'].unit)

            return pd.Series({'flux': "%s %s" % (rep, unit_rep)})

        if mle_results is not None:

            # Format the errors and display the resulting data frame

            display(mle_results.apply(_format_error, axis=1))

            # Return the dataframe
            return mle_results

        elif bayes_results is not None:

            # Format the errors and display the resulting data frame

            display(bayes_results.apply(_format_error, axis=1))

            # Return the dataframe
            return bayes_results


    def get_equal_tailed_interval(self,parameter,cl=0.68):
        """

        returns the equal tailed interval for the parameter

        :param parameter_path: path of the parameter or parameter instance
        :param cl: credible interval to obtain
        :return: (low bound, high bound)
        """

        if isinstance(parameter,Parameter):


            path = parameter.path

        else:

            path = parameter



        variates = self.get_variates(path)

        return variates.equal_tail_interval(cl)


class BayesianResults(_AnalysisResults):
    """
    Store results of a Bayesian analysis (i.e., the samples) and allow for computation with them and "error propagation"

    :param optimized_model: a Model instance with the MAP values of the parameters. A clone will be stored within
    the class, so there is no need to clone it before hand
    :type optimized_model: astromodels.Model
    :param samples: the samples for the parameters
    :type samples: np.ndarray
    :param posterior_values: a dictionary containing the posterior values for the different datasets at the HPD
    :type posterior_values: dict
    """

    def __init__(self, optimized_model, samples, posterior_values, statistical_measures):

        super(BayesianResults, self).__init__(optimized_model, samples, posterior_values, 'Bayesian', statistical_measures)


    def get_correlation_matrix(self):
        """
        Estimate the covariance matrix from the samples

        :return: the correlation matrix
        """

        # Here we need to estimate the covariance from the samples, then compute the correlation matrix

        covariance = self.estimate_covariance_matrix()

        return self._get_correlation_matrix(covariance)

    def get_statistic_frame(self):

        return self._get_statistic_frame(name='-log(posterior)')

    def display(self, display_correlation=False, error_type="equal tail", cl=0.68):

        best_fit_table = self._get_results_table(error_type, cl)

        print("Maximum a posteriori probability (MAP) point:\n")

        best_fit_table.display()

        if display_correlation:

            corr_matrix = NumericMatrix(self.get_correlation_matrix())

            for col in corr_matrix.colnames:
                corr_matrix[col].format = '2.2f'

            print("\nCorrelation matrix:\n")

            display(corr_matrix)

        print("\nValues of -log(posterior) at the minimum:\n")

        display(self.get_statistic_frame())

        print("\nValues of statistical measures:\n")

        display(self.get_statistic_measure_frame())

    def corner_plot(self, renamed_parameters=None, **kwargs):
        """
        Produce the corner plot showing the marginal distributions in one and two directions.

        :param renamed_parameters: a python dictionary of parameters to rename.
             Useful when e.g. spectral indices in models have different names but you wish to compare them. Format is
             {'old label': 'new label'}, where 'old label' is the full path of the parameter
        :param kwargs: arguments to be passed to the corner function
        :return: a matplotlib.figure instance
        """

        assert len(self._free_parameters.keys()) == self._samples_transposed.T[0].shape[0], ("Mismatch between sample"
                                                                                             " dimensions and number of free"
                                                                                             " parameters")

        labels = []
        priors = []

        for i, (parameter_name, parameter) in enumerate(self._free_parameters.items()):

            short_name = parameter_name.split(".")[-1]

            labels.append(short_name)

            # If the user has provided custom names, use them

            if renamed_parameters is not None:

                if parameter.path in renamed_parameters:

                    labels[-1] = renamed_parameters[parameter.path]

            priors.append(self._optimized_model.parameters[parameter_name].prior)

        # default arguments
        default_args = {'show_titles': True, 'title_fmt': ".2g", 'labels': labels,
                        'quantiles': [0.16, 0.50, 0.84]}

        # Update the default arguents with the one provided (if any). Note that .update also adds new keywords,
        # if they weren't present in the original dictionary, so you can use any option in kwargs, not just
        # the one in default_args
        default_args.update(kwargs)

        fig = corner(self._samples_transposed.T, **default_args)

        return fig

    def corner_plot_cc(self, parameters=None, renamed_parameters=None, **cc_kwargs):
        """
        Corner plots using chainconsumer which allows for nicer plotting of
        marginals
        see: https://samreay.github.io/ChainConsumer/chain_api.html#chainconsumer.ChainConsumer.configure
        for all options
        :param parameters: list of parameters to plot
        :param renamed_parameters: a python dictionary of parameters to rename.
             Useful when e.g. spectral indices in models have different names but you wish to compare them. Format is
             {'old label': 'new label'}
        :param **cc_kwargs: chainconsumer general keyword arguments
        :return fig:
        """


        if not has_chainconsumer:
            raise RuntimeError("You must have chainconsumer installed to use this function: pip install chainconsumer")

        # these are the keywords for the plot command

        _default_plot_args = {'truth': None,
                              'figsize': 'GROW',
                              'filename': None,
                              'display': False,
                              'legend': None}
        keys = cc_kwargs.keys()
        for key in keys:

            if key in _default_plot_args:
                _default_plot_args[key] = cc_kwargs.pop(key)

        labels = []
        priors = []

        for i, (parameter_name, parameter) in enumerate(self._free_parameters.iteritems()):
            short_name = parameter_name.split(".")[-1]

            labels.append(short_name)

            priors.append(self._optimized_model.parameters[parameter_name].prior)

        # Rename the parameters if needed.

        if renamed_parameters is not None:

            for old_label, new_label in renamed_parameters.iteritems():

                for i, _ in enumerate(labels):

                    if labels[i] == old_label:
                        labels[i] = new_label

        # Must remove underscores!

        for i, val, in enumerate(labels):

            if '$' not in labels[i]:
                labels[i] = val.replace('_', '')

        cc = chainconsumer.ChainConsumer()

        cc.add_chain(self._samples_transposed.T, parameters=labels)

        if not cc_kwargs:
            cc_kwargs = threeML_config['bayesian']['chain consumer style']

        cc.configure(**cc_kwargs)
        fig = cc.plot(parameters=parameters, **_default_plot_args)

        return fig

    def comparison_corner_plot(self, *other_fits, **kwargs):
        """
        Create a corner plot from many different fits which allow for co-plotting of parameters marginals.

        :param other_fits: other fitted results
        :param parameters: parameters to plot
        :param renamed_parameters: a python dictionary of parameters to rename.
             Useful when e.g. spectral indices in models have different names but you wish to compare them. Format is
             {'old label': 'new label'}
        :param names: (optional) name for each chain first name is this chain followed by each added chain
        :param kwargs: chain consumer kwargs
        :return:

        Returns:

        """


        if not has_chainconsumer:
            raise RuntimeError("You must have chainconsumer installed to use this function")

        cc = chainconsumer.ChainConsumer()

        # these are the keywords for the plot command

        _default_plot_args = {'truth': None,
                              'figsize': 'GROW',
                              'parameters': None,
                              'filename': None,
                              'display': False,
                              'legend': None}

        keys = kwargs.keys()

        for key in keys:

            if key in _default_plot_args:
                _default_plot_args[key] = kwargs.pop(key)

        # allows us to name chains

        if 'names' in kwargs:

            names = kwargs.pop('names')

            assert len(names) == len(other_fits) + 1, 'you have %d chains but %d names' % (
                len(other_fits) + 1, len(names))

        else:

            names = None

        if 'renamed_parameters' in kwargs:

            renamed_parameters = kwargs.pop('renamed_parameters')

        else:

            renamed_parameters = None

        for j, other_fit in enumerate(other_fits):

            if other_fit.samples is not None:
                assert len(other_fit._free_parameters.keys()) == other_fit.samples.T[0].shape[0], (
                    "Mismatch between sample"



                    " dimensions and number of free"
                    " parameters")

            labels_other = []
            # priors_other = []

            for i, (parameter_name, parameter) in enumerate(other_fit._free_parameters.iteritems()):
                short_name = parameter_name.split(".")[-1]

                labels_other.append(short_name)

                # priors_other.append(other_fit._likelihood_model.parameters[parameter_name].prior)

            # Rename any parameters so that they can be plotted together.
            # A dictionary is passed with keys = old label values = new label.

            if renamed_parameters is not None:

                for old_label, new_label in renamed_parameters.iteritems():

                    for i, _ in enumerate(labels_other):

                        if labels_other[i] == old_label:
                            labels_other[i] = new_label

            # Must remove underscores!

            for i, val, in enumerate(labels_other):

                if '$' not in labels_other[i]:
                    labels_other[i] = val.replace('_', ' ')

            if names is not None:

                cc.add_chain(other_fit.samples.T, parameters=labels_other, name=names[j + 1])

            else:

                cc.add_chain(other_fit.samples.T, parameters=labels_other)

        labels = []
        # priors = []

        for i, (parameter_name, parameter) in enumerate(self._free_parameters.iteritems()):
            short_name = parameter_name.split(".")[-1]

            labels.append(short_name)

            # priors.append(self._optimized_model.parameters[parameter_name].prior)

        if renamed_parameters is not None:

            for old_label, new_label in renamed_parameters.iteritems():

                for i, _ in enumerate(labels):

                    if labels[i] == old_label:
                        labels[i] = new_label

        # Must remove underscores!

        for i, val, in enumerate(labels):

            if '$' not in labels[i]:
                labels[i] = val.replace('_', ' ')

        if names is not None:

            cc.add_chain(self._samples_transposed.T, parameters=labels, name=names[0])

        else:

            cc.add_chain(self._samples_transposed.T, parameters=labels)

        # should only be the cc kwargs

        cc.configure(**kwargs)
        fig = cc.plot(**_default_plot_args)

        return fig

    def get_highest_density_posterior_interval(self,parameter,cl=0.68):
        """

        returns the highest density posterior interval for that parameter

        :param parameter_path: path of the parameter or parameter instance
        :param cl: credible interval to obtain
        :return: (low bound, high bound)
        """

        if isinstance(parameter,Parameter):


            path = parameter.path

        else:

            path = parameter



        variates = self.get_variates(path)

        return variates.highest_posterior_density_interval(cl)







class MLEResults(_AnalysisResults):
    """
    Build the _AnalysisResults object starting from a covariance matrix.


    :param optimized_model: best fit model
    :type optimized_model:astromodels.Model
    :param covariance_matrix:
    :type covariance_matrix: np.ndarray
    :param likelihood_values:
    :type likelihood_values: dict
    :param n_samples: Number of samples to use
    :type n_samples: int
    :return: an _AnalysisResults instance
    """

    def __init__(self, optimized_model, covariance_matrix, likelihood_values, n_samples=5000, statistical_measures=None):

        # Generate samples for each parameter accounting for their covariance

        # Force covariance into proper type
        covariance_matrix = np.array(covariance_matrix, float, copy=True)

        # Get the best fit value for each parameter
        values = map(lambda x:x._get_internal_value(), optimized_model.free_parameters.values())

        # This is the expected shape for the covariance matrix

        expected_shape = (len(values), len(values))

        if covariance_matrix.shape != ():

            assert covariance_matrix.shape == expected_shape, "Covariance matrix has wrong shape. " \
                                                              "Got %s, should be %s" % (covariance_matrix.shape,
                                                                                        expected_shape)

            assert np.all(np.isfinite(covariance_matrix)), "Covariance matrix contains Nan or inf. Cannot continue."

            # Generate samples from the multivariate normal distribution, i.e., accounting for the covariance of the
            # parameters

            samples = np.random.multivariate_normal(np.array(values).T, covariance_matrix, n_samples)

        else:

            # No error information, just make duplicates of the values
            samples = np.ones((n_samples, len(values))) * np.array(values)

            # Make a fake covariance matrix
            covariance_matrix = np.zeros(expected_shape)

        # Now reject the samples outside of the boundaries. If we reject more than 1% we warn the user

        # Gather boundaries
        # NOTE: every None boundary will become nan thanks to the casting to float
        low_bounds = np.array(map(lambda x: x._get_internal_min_value(), optimized_model.free_parameters.values()), float)
        hi_bounds = np.array(map(lambda x: x._get_internal_max_value(), optimized_model.free_parameters.values()), float)

        # Fix all nans
        low_bounds[np.isnan(low_bounds)] = -np.inf
        hi_bounds[np.isnan(hi_bounds)] = np.inf

        to_be_kept_mask = np.ones(samples.shape[0], bool)

        for i, sample in enumerate(samples):

            if np.any(sample > hi_bounds) or np.any(sample < low_bounds):
                # Remove this sample
                to_be_kept_mask[i] = False

        # Compute how many samples we have removed
        n_removed_samples = samples.shape[0] - np.sum(to_be_kept_mask)

        # Warn the user if more than 1% of the samples have been lost

        if n_removed_samples > samples.shape[0] / 100.0:
            custom_warnings.warn("%s percent of samples have been thrown away because they failed the constraints "
                                 "on the parameters. This results might not be suitable for error propagation. "
                                 "Enlarge the boundaries until you loose less than 1 percent of the samples." %
                                 (float(n_removed_samples) / samples.shape[0] * 100.0))

        # Now remove them
        samples = samples[to_be_kept_mask, :]

        # Now transform in the external space
        for i, parameter in enumerate(optimized_model.free_parameters.values()):

            if parameter.has_transformation():

                samples[:, i] = parameter.transformation.backward(samples[:, i])

        # Finally build the class

        super(MLEResults, self).__init__(optimized_model, samples, likelihood_values, "MLE", statistical_measures)

        # Store the covariance matrix

        self._covariance_matrix = covariance_matrix

    @property
    def covariance_matrix(self):
        """
        Returns the covariance matrix.

        :return: covariance matrix or None (if the class was built from samples.
                 Use estimate_covariance_matrix in that case)
        """

        return self._covariance_matrix

    def get_correlation_matrix(self):
        """
        Compute correlation matrix

        :return: the correlation matrix
        """

        return self._get_correlation_matrix(self._covariance_matrix)

    def get_statistic_frame(self):

        return self._get_statistic_frame(name='-log(likelihood)')

    def display(self, display_correlation=True, cl=0.68):

        best_fit_table = self._get_results_table(error_type="covariance", cl=cl, covariance=self.covariance_matrix)

        print("Best fit values:\n")

        best_fit_table.display()

        if display_correlation:

            corr_matrix = NumericMatrix(self.get_correlation_matrix())

            for col in corr_matrix.colnames:
                corr_matrix[col].format = '2.2f'

            print("\nCorrelation matrix:\n")

            display(corr_matrix)

        print("\nValues of -log(likelihood) at the minimum:\n")

        display(self.get_statistic_frame())

        print("\nValues of statistical measures:\n")

        display(self.get_statistic_measure_frame())


class AnalysisResultsSet(collections.Sequence):
    """
    A container for results which behaves like a list (but you cannot add/remove elements).

    You can index (analysis_set[0]), iterate (for item in analysis_set) and measure with len()
    """

    def __init__(self, results):

        self._results = results

    def __getitem__(self, item):

        return self._results[item]

    def __len__(self):

        return len(self._results)

    def set_x(self, name, x, unit=None):
        """
        Associate the provided x with these results. The values in x will be written in the SEQUENCE extension when
        saving these results to a FITS file.

        :param name: a name for this sequence (for example, "time" or "energy"). Please use only letters and numbers
        (no special characters)
        :param x:
        :param unit: unit for x (like "s" for seconds, or a astropy.units.Unit instance)
        :return:
        """

        assert len(x) == len(self), "Wrong number of bounds (%i, should be %i)" % (len(x), len(self))

        if unit is not None:

            unit = u.Unit(unit)

            data_tuple = (('VALUE', x * unit),)

        else:

            data_tuple = (('VALUE', x),)

        self.characterize_sequence(name, data_tuple)

    def set_bins(self, name, lower_bounds, upper_bounds, unit=None):
        """
        Associate the provided bins with these results. These bins will be written in the SEQUENCE extension when
        saving these results to a FITS file

        :param name: a name for these bins (for example, "time" or "energy"). Please use only letters and numbers
        (no special characters)
        :param lower_bounds:
        :param upper_bounds:
        :param unit: unit for the boundaries (like "s" for seconds, or a astropy.units.Unit instance)
        :return:
        """

        assert len(upper_bounds) == len(lower_bounds), "Upper and lower bounds must have the same length"

        assert len(upper_bounds) == len(self), "Wrong number of bounds (%i, should be %i)" % (len(upper_bounds),
                                                                                              len(self))

        if unit is not None:

            unit = u.Unit(unit)

            data_tuple = (('LOWER_BOUND', lower_bounds * unit),
                          ('UPPER_BOUND', upper_bounds * unit))

        else:

            data_tuple = (('LOWER_BOUND', lower_bounds),
                          ('UPPER_BOUND', upper_bounds))

        self.characterize_sequence(name, data_tuple)

    def characterize_sequence(self, name, data_tuple):
        """
        Characterize the sequence of these results. The provided data frame will be saved along with the results
        in the "SEQUENCE" extension to allow the interpretation of the results.

        This method is completely general, and allow for a lot of flexibility.

        If this is a binned analysis and you only want to save the lower and upper bound of the bins, use
        set_bins instead.

        If you only want to associate one quantity for each entry, use set_x.
        """

        self._sequence_name = str(name)

        for i, this_tuple in enumerate(data_tuple):
            assert len(this_tuple[1]) == len(self), "Column %i in tuple has length of " \
                                                    "%i (should be %i)" % (i, len(data_tuple), len(self))

        self._sequence_tuple = data_tuple

    def write_to(self, filename, overwrite=False):
        """
        Write this set of results to a FITS file.

        :param filename: name for the output file
        :param overwrite: True or False
        :return: None
        """

        if not hasattr(self, "_sequence_name"):
            # The user didn't specify what this sequence is

            # Make the default sequence
            frame_tuple = (('VALUE', range(len(self))),)

            self.characterize_sequence("unspecified", frame_tuple)

        fits = AnalysisResultsFITS(*self, sequence_tuple=self._sequence_tuple, sequence_name=self._sequence_name)

        fits.writeto(sanitize_filename(filename), overwrite=overwrite)
