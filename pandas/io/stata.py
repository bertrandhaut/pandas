"""
Module contains tools for processing Stata files into DataFrames

The StataReader below was originally written by Joe Presbrey as part of PyDTA.
It has been extended and improved by Skipper Seabold from the Statsmodels
project who also developed the StataWriter and was finally added to pandas in
an once again improved version.

You can find more information on http://presbrey.mit.edu/PyDTA and
http://statsmodels.sourceforge.net/devel/
"""
import numpy as np

import sys
import struct
from dateutil.relativedelta import relativedelta
from pandas.core.base import StringMixin
from pandas.core.frame import DataFrame
from pandas.core.series import Series
from pandas.core.categorical import Categorical
import datetime
from pandas import compat, to_timedelta, to_datetime, isnull, DatetimeIndex
from pandas.compat import lrange, lmap, lzip, text_type, string_types, range, \
    zip
import pandas.core.common as com
from pandas.io.common import get_filepath_or_buffer
from pandas.lib import max_len_string_array, infer_dtype
from pandas.tslib import NaT, Timestamp

def read_stata(filepath_or_buffer, convert_dates=True,
               convert_categoricals=True, encoding=None, index=None,
               convert_missing=False):
    """
    Read Stata file into DataFrame

    Parameters
    ----------
    filepath_or_buffer : string or file-like object
        Path to .dta file or object implementing a binary read() functions
    convert_dates : boolean, defaults to True
        Convert date variables to DataFrame time values
    convert_categoricals : boolean, defaults to True
        Read value labels and convert columns to Categorical/Factor variables
    encoding : string, None or encoding
        Encoding used to parse the files. Note that Stata doesn't
        support unicode. None defaults to cp1252.
    index : identifier of index column
        identifier of column that should be used as index of the DataFrame
    convert_missing : boolean, defaults to False
        Flag indicating whether to convert missing values to their Stata
        representations.  If False, missing values are replaced with nans.
        If True, columns containing missing values are returned with
        object data types and missing values are represented by
        StataMissingValue objects.
    """
    reader = StataReader(filepath_or_buffer, encoding)

    return reader.data(convert_dates,
                       convert_categoricals,
                       index,
                       convert_missing)

_date_formats = ["%tc", "%tC", "%td", "%d", "%tw", "%tm", "%tq", "%th", "%ty"]


stata_epoch = datetime.datetime(1960, 1, 1)


def _stata_elapsed_date_to_datetime_vec(dates, fmt):
    """
    Convert from SIF to datetime. http://www.stata.com/help.cgi?datetime

    Parameters
    ----------
    dates : Series
        The Stata Internal Format date to convert to datetime according to fmt
    fmt : str
        The format to convert to. Can be, tc, td, tw, tm, tq, th, ty
        Returns

    Returns
    -------
    converted : Series
        The converted dates

    Examples
    --------
    >>> import pandas as pd
    >>> dates = pd.Series([52])
    >>> _stata_elapsed_date_to_datetime_vec(dates , "%tw")
    0   1961-01-01
    dtype: datetime64[ns]

    Notes
    -----
    datetime/c - tc
        milliseconds since 01jan1960 00:00:00.000, assuming 86,400 s/day
    datetime/C - tC - NOT IMPLEMENTED
        milliseconds since 01jan1960 00:00:00.000, adjusted for leap seconds
    date - td
        days since 01jan1960 (01jan1960 = 0)
    weekly date - tw
        weeks since 1960w1
        This assumes 52 weeks in a year, then adds 7 * remainder of the weeks.
        The datetime value is the start of the week in terms of days in the
        year, not ISO calendar weeks.
    monthly date - tm
        months since 1960m1
    quarterly date - tq
        quarters since 1960q1
    half-yearly date - th
        half-years since 1960h1 yearly
    date - ty
        years since 0000

    If you don't have pandas with datetime support, then you can't do
    milliseconds accurately.
    """
    MIN_YEAR, MAX_YEAR = Timestamp.min.year, Timestamp.max.year
    MAX_DAY_DELTA = (Timestamp.max - datetime.datetime(1960, 1, 1)).days
    MIN_DAY_DELTA = (Timestamp.min - datetime.datetime(1960, 1, 1)).days
    MIN_MS_DELTA = MIN_DAY_DELTA * 24 * 3600 * 1000
    MAX_MS_DELTA = MAX_DAY_DELTA * 24 * 3600 * 1000

    def convert_year_month_safe(year, month):
        """
        Convert year and month to datetimes, using pandas vectorized versions
        when the date range falls within the range supported by pandas.  Other
        wise it falls back to a slower but more robust method using datetime.
        """
        if year.max() < MAX_YEAR and year.min() > MIN_YEAR:
            return to_datetime(100 * year + month, format='%Y%m')
        else:
            return Series(
                [datetime.datetime(y, m, 1) for y, m in zip(year, month)])

    def convert_year_days_safe(year, days):
        """
        Converts year (e.g. 1999) and days since the start of the year to a
        datetime or datetime64 Series
        """
        if year.max() < (MAX_YEAR - 1) and year.min() > MIN_YEAR:
            return to_datetime(year, format='%Y') + to_timedelta(days, unit='d')
        else:
            value = [datetime.datetime(y, 1, 1) + relativedelta(days=int(d)) for
                     y, d in zip(year, days)]
            return Series(value)

    def convert_delta_safe(base, deltas, unit):
        """
        Convert base dates and deltas to datetimes, using pandas vectorized
        versions if the deltas satisfy restrictions required to be expressed
        as dates in pandas.
        """
        if unit == 'd':
            if deltas.max() > MAX_DAY_DELTA or deltas.min() < MIN_DAY_DELTA:
                values = [base + relativedelta(days=int(d)) for d in deltas]
                return Series(values)
        elif unit == 'ms':
            if deltas.max() > MAX_MS_DELTA or deltas.min() < MIN_MS_DELTA:
                values = [base + relativedelta(microseconds=(int(d) * 1000)) for
                          d in deltas]
                return Series(values)
        else:
            raise ValueError('format not understood')

        base = to_datetime(base)
        deltas = to_timedelta(deltas, unit=unit)
        return base + deltas

    # TODO: If/when pandas supports more than datetime64[ns], this should be improved to use correct range, e.g. datetime[Y] for yearly
    bad_locs = np.isnan(dates)
    has_bad_values = False
    if bad_locs.any():
        has_bad_values = True
        data_col = Series(dates)
        data_col[bad_locs] = 1.0  # Replace with NaT
    dates = dates.astype(np.int64)

    if fmt in ["%tc", "tc"]:  # Delta ms relative to base
        base = stata_epoch
        ms = dates
        conv_dates = convert_delta_safe(base, ms, 'ms')
    elif fmt in ["%tC", "tC"]:
        from warnings import warn

        warn("Encountered %tC format. Leaving in Stata Internal Format.")
        conv_dates = Series(dates, dtype=np.object)
        if has_bad_values:
            conv_dates[bad_locs] = np.nan
        return conv_dates
    elif fmt in ["%td", "td", "%d", "d"]:  # Delta days relative to base
        base = stata_epoch
        days = dates
        conv_dates = convert_delta_safe(base, days, 'd')
    elif fmt in ["%tw", "tw"]:  # does not count leap days - 7 days is a week
        year = stata_epoch.year + dates // 52
        days = (dates % 52) * 7
        conv_dates = convert_year_days_safe(year, days)
    elif fmt in ["%tm", "tm"]:  # Delta months relative to base
        year = stata_epoch.year + dates // 12
        month = (dates % 12) + 1
        conv_dates = convert_year_month_safe(year, month)
    elif fmt in ["%tq", "tq"]:  # Delta quarters relative to base
        year = stata_epoch.year + dates // 4
        month = (dates % 4) * 3 + 1
        conv_dates = convert_year_month_safe(year, month)
    elif fmt in ["%th", "th"]:  # Delta half-years relative to base
        year = stata_epoch.year + dates // 2
        month = (dates % 2) * 6 + 1
        conv_dates = convert_year_month_safe(year, month)
    elif fmt in ["%ty", "ty"]:  # Years -- not delta
        year = dates
        month = np.ones_like(dates)
        conv_dates = convert_year_month_safe(year, month)
    else:
        raise ValueError("Date fmt %s not understood" % fmt)

    if has_bad_values:  # Restore NaT for bad values
        conv_dates[bad_locs] = NaT
    return conv_dates


def _datetime_to_stata_elapsed_vec(dates, fmt):
    """
    Convert from datetime to SIF. http://www.stata.com/help.cgi?datetime

    Parameters
    ----------
    dates : Series
        Series or array containing datetime.datetime or datetime64[ns] to
        convert to the Stata Internal Format given by fmt
    fmt : str
        The format to convert to. Can be, tc, td, tw, tm, tq, th, ty
    """
    index = dates.index
    NS_PER_DAY = 24 * 3600 * 1000 * 1000 * 1000
    US_PER_DAY = NS_PER_DAY / 1000

    def parse_dates_safe(dates, delta=False, year=False, days=False):
        d = {}
        if com.is_datetime64_dtype(dates.values):
            if delta:
                delta = dates - stata_epoch
                d['delta'] = delta.values.astype(
                    np.int64) // 1000  # microseconds
            if days or year:
                dates = DatetimeIndex(dates)
                d['year'], d['month'] = dates.year, dates.month
            if days:
                days = (dates.astype(np.int64) -
                        to_datetime(d['year'], format='%Y').astype(np.int64))
                d['days'] = days // NS_PER_DAY

        elif infer_dtype(dates) == 'datetime':
            if delta:
                delta = dates.values - stata_epoch
                f = lambda x: \
                    US_PER_DAY * x.days + 1000000 * x.seconds + x.microseconds
                v = np.vectorize(f)
                d['delta'] = v(delta)
            if year:
                year_month = dates.apply(lambda x: 100 * x.year + x.month)
                d['year'] = year_month.values // 100
                d['month'] = (year_month.values - d['year'] * 100)
            if days:
                f = lambda x: (x - datetime.datetime(x.year, 1, 1)).days
                v = np.vectorize(f)
                d['days'] = v(dates)
        else:
            raise ValueError('Columns containing dates must contain either '
                             'datetime64, datetime.datetime or null values.')

        return DataFrame(d, index=index)

    bad_loc = isnull(dates)
    index = dates.index
    if bad_loc.any():
        dates = Series(dates)
        if com.is_datetime64_dtype(dates):
            dates[bad_loc] = to_datetime(stata_epoch)
        else:
            dates[bad_loc] = stata_epoch

    if fmt in ["%tc", "tc"]:
        d = parse_dates_safe(dates, delta=True)
        conv_dates = d.delta / 1000
    elif fmt in ["%tC", "tC"]:
        from warnings import warn
        warn("Stata Internal Format tC not supported.")
        conv_dates = dates
    elif fmt in ["%td", "td"]:
        d = parse_dates_safe(dates, delta=True)
        conv_dates = d.delta // US_PER_DAY
    elif fmt in ["%tw", "tw"]:
        d = parse_dates_safe(dates, year=True, days=True)
        conv_dates = (52 * (d.year - stata_epoch.year) + d.days // 7)
    elif fmt in ["%tm", "tm"]:
        d = parse_dates_safe(dates, year=True)
        conv_dates = (12 * (d.year - stata_epoch.year) + d.month - 1)
    elif fmt in ["%tq", "tq"]:
        d = parse_dates_safe(dates, year=True)
        conv_dates = 4 * (d.year - stata_epoch.year) + (d.month - 1) // 3
    elif fmt in ["%th", "th"]:
        d = parse_dates_safe(dates, year=True)
        conv_dates = 2 * (d.year - stata_epoch.year) + \
                         (d.month > 6).astype(np.int)
    elif fmt in ["%ty", "ty"]:
        d = parse_dates_safe(dates, year=True)
        conv_dates = d.year
    else:
        raise ValueError("fmt %s not understood" % fmt)

    conv_dates = Series(conv_dates, dtype=np.float64)
    missing_value = struct.unpack('<d', b'\x00\x00\x00\x00\x00\x00\xe0\x7f')[0]
    conv_dates[bad_loc] = missing_value

    return Series(conv_dates, index=index)


excessive_string_length_error = """
Fixed width strings in Stata .dta files are limited to 244 (or fewer) characters.
Column '%s' does not satisfy this restriction.
"""

class PossiblePrecisionLoss(Warning):
    pass


precision_loss_doc = """
Column converted from %s to %s, and some data are outside of the lossless
conversion range. This may result in a loss of precision in the saved data.
"""


class InvalidColumnName(Warning):
    pass


invalid_name_doc = """
Not all pandas column names were valid Stata variable names.
The following replacements have been made:

    {0}

If this is not what you expect, please make sure you have Stata-compliant
column names in your DataFrame (strings only, max 32 characters, only alphanumerics and
underscores, no Stata reserved words)
"""


def _cast_to_stata_types(data):
    """Checks the dtypes of the columns of a pandas DataFrame for
    compatibility with the data types and ranges supported by Stata, and
    converts if necessary.

    Parameters
    ----------
    data : DataFrame
        The DataFrame to check and convert

    Notes
    -----
    Numeric columns in Stata must be one of int8, int16, int32, float32 or
    float64, with some additional value restrictions.  int8 and int16 columns
    are checked for violations of the value restrictions and
    upcast if needed.  int64 data is not usable in Stata, and so it is
    downcast to int32 whenever the value are in the int32 range, and
    sidecast to float64 when larger than this range.  If the int64 values
    are outside of the range of those perfectly representable as float64 values,
    a warning is raised.

    bool columns are cast to int8.  uint colums are converted to int of the same
    size if there is no loss in precision, other wise are upcast to a larger
    type.  uint64 is currently not supported since it is concerted to object in
    a DataFrame.
    """
    ws = ''
    #                  original, if small, if large
    conversion_data = ((np.bool, np.int8, np.int8),
                       (np.uint8, np.int8, np.int16),
                       (np.uint16, np.int16, np.int32),
                       (np.uint32, np.int32, np.int64))

    for col in data:
        dtype = data[col].dtype
        # Cast from unsupported types to supported types
        for c_data in conversion_data:
            if dtype == c_data[0]:
                if data[col].max() <= np.iinfo(c_data[1]).max:
                    dtype = c_data[1]
                else:
                    dtype = c_data[2]
                if c_data[2] == np.float64:  # Warn if necessary
                        if data[col].max() >= 2 ** 53:
                            ws = precision_loss_doc % ('uint64', 'float64')

                data[col] = data[col].astype(dtype)


        # Check values and upcast if necessary
        if dtype == np.int8:
            if data[col].max() > 100 or data[col].min() < -127:
                data[col] = data[col].astype(np.int16)
        elif dtype == np.int16:
            if data[col].max() > 32740 or data[col].min() < -32767:
                data[col] = data[col].astype(np.int32)
        elif dtype == np.int64:
            if data[col].max() <= 2147483620 and data[col].min() >= -2147483647:
                data[col] = data[col].astype(np.int32)
            else:
                data[col] = data[col].astype(np.float64)
                if data[col].max() >= 2 ** 53 or data[col].min() <= -2 ** 53:
                    ws = precision_loss_doc % ('int64', 'float64')

    if ws:
        import warnings

        warnings.warn(ws, PossiblePrecisionLoss)

    return data


class StataMissingValue(StringMixin):
    """
    An observation's missing value.

    Parameters
    -----------
    value : int8, int16, int32, float32 or float64
        The Stata missing value code

    Attributes
    ----------
    string : string
        String representation of the Stata missing value
    value : int8, int16, int32, float32 or float64
        The original encoded missing value

    Notes
    -----
    More information: <http://www.stata.com/help.cgi?missing>

    Integer missing values make the code '.', '.a', ..., '.z' to the ranges
    101 ... 127 (for int8), 32741 ... 32767  (for int16) and 2147483621 ...
    2147483647 (for int32).  Missing values for floating point data types are
    more complex but the pattern is simple to discern from the following table.

    np.float32 missing values (float in Stata)
    0000007f    .
    0008007f    .a
    0010007f    .b
    ...
    00c0007f    .x
    00c8007f    .y
    00d0007f    .z

    np.float64 missing values (double in Stata)
    000000000000e07f    .
    000000000001e07f    .a
    000000000002e07f    .b
    ...
    000000000018e07f    .x
    000000000019e07f    .y
    00000000001ae07f    .z
    """

    # Construct a dictionary of missing values
    MISSING_VALUES = {}
    bases = (101, 32741, 2147483621)
    for b in bases:
        MISSING_VALUES[b] = '.'
        for i in range(1, 27):
            MISSING_VALUES[i + b] = '.' + chr(96 + i)

    base = b'\x00\x00\x00\x7f'
    increment = struct.unpack('<i', b'\x00\x08\x00\x00')[0]
    for i in range(27):
        value = struct.unpack('<f', base)[0]
        MISSING_VALUES[value] = '.'
        if i > 0:
            MISSING_VALUES[value] += chr(96 + i)
        int_value = struct.unpack('<i', struct.pack('<f', value))[0] + increment
        base = struct.pack('<i', int_value)

    base = b'\x00\x00\x00\x00\x00\x00\xe0\x7f'
    increment = struct.unpack('q', b'\x00\x00\x00\x00\x00\x01\x00\x00')[0]
    for i in range(27):
        value = struct.unpack('<d', base)[0]
        MISSING_VALUES[value] = '.'
        if i > 0:
            MISSING_VALUES[value] += chr(96 + i)
        int_value = struct.unpack('q', struct.pack('<d', value))[0] + increment
        base = struct.pack('q', int_value)

    def __init__(self, value):
        self._value = value
        self._str = self.MISSING_VALUES[value]

    string = property(lambda self: self._str,
                      doc="The Stata representation of the missing value: "
                          "'.', '.a'..'.z'")
    value = property(lambda self: self._value,
                     doc='The binary representation of the missing value.')

    def __unicode__(self):
        return self.string

    def __repr__(self):
        # not perfect :-/
        return "%s(%s)" % (self.__class__, self)

    def __eq__(self, other):
        return (isinstance(other, self.__class__)
                and self.string == other.string and self.value == other.value)


class StataParser(object):
    _default_encoding = 'cp1252'

    def __init__(self, encoding):
        self._encoding = encoding

        #type          code.
        #--------------------
        #str1        1 = 0x01
        #str2        2 = 0x02
        #...
        #str244    244 = 0xf4
        #byte      251 = 0xfb  (sic)
        #int       252 = 0xfc
        #long      253 = 0xfd
        #float     254 = 0xfe
        #double    255 = 0xff
        #--------------------
        #NOTE: the byte type seems to be reserved for categorical variables
        # with a label, but the underlying variable is -127 to 100
        # we're going to drop the label and cast to int
        self.DTYPE_MAP = \
            dict(
                lzip(range(1, 245), ['a' + str(i) for i in range(1, 245)]) +
                [
                    (251, np.int8),
                    (252, np.int16),
                    (253, np.int32),
                    (254, np.float32),
                    (255, np.float64)
                ]
            )
        self.DTYPE_MAP_XML = \
            dict(
                [
                    (32768, np.string_),
                    (65526, np.float64),
                    (65527, np.float32),
                    (65528, np.int32),
                    (65529, np.int16),
                    (65530, np.int8)
                ]
            )
        self.TYPE_MAP = lrange(251) + list('bhlfd')
        self.TYPE_MAP_XML = \
            dict(
                [
                    (65526, 'd'),
                    (65527, 'f'),
                    (65528, 'l'),
                    (65529, 'h'),
                    (65530, 'b')
                ]
            )
        #NOTE: technically, some of these are wrong. there are more numbers
        # that can be represented. it's the 27 ABOVE and BELOW the max listed
        # numeric data type in [U] 12.2.2 of the 11.2 manual
        float32_min = b'\xff\xff\xff\xfe'
        float32_max = b'\xff\xff\xff\x7e'
        float64_min = b'\xff\xff\xff\xff\xff\xff\xef\xff'
        float64_max = b'\xff\xff\xff\xff\xff\xff\xdf\x7f'
        self.VALID_RANGE = \
            {
                'b': (-127, 100),
                'h': (-32767, 32740),
                'l': (-2147483647, 2147483620),
                'f': (np.float32(struct.unpack('<f', float32_min)[0]),
                      np.float32(struct.unpack('<f', float32_max)[0])),
                'd': (np.float64(struct.unpack('<d', float64_min)[0]),
                      np.float64(struct.unpack('<d', float64_max)[0]))
            }

        self.OLD_TYPE_MAPPING = \
            {
                'i': 252,
                'f': 254,
                'b': 251
            }
        # These missing values are the generic '.' in Stata, and are used
        # to replace nans
        self.MISSING_VALUES = \
            {
                'b': 101,
                'h': 32741,
                'l': 2147483621,
                'f': np.float32(struct.unpack('<f', b'\x00\x00\x00\x7f')[0]),
                'd': np.float64(struct.unpack('<d', b'\x00\x00\x00\x00\x00\x00\xe0\x7f')[0])
            }
        self.NUMPY_TYPE_MAP = \
        {
                'b': 'i1',
                'h': 'i2',
                'l': 'i4',
                'f': 'f4',
                'd': 'f8'
        }

        # Reserved words cannot be used as variable names
        self.RESERVED_WORDS = ('aggregate', 'array', 'boolean', 'break',
                               'byte', 'case', 'catch', 'class', 'colvector',
                               'complex', 'const', 'continue', 'default',
                               'delegate', 'delete', 'do', 'double', 'else',
                               'eltypedef', 'end', 'enum', 'explicit',
                               'export', 'external', 'float', 'for', 'friend',
                               'function', 'global', 'goto', 'if', 'inline',
                               'int', 'local', 'long', 'NULL', 'pragma',
                               'protected', 'quad', 'rowvector', 'short',
                               'typedef', 'typename', 'virtual')

    def _decode_bytes(self, str, errors=None):
        if compat.PY3 or self._encoding is not None:
            return str.decode(self._encoding, errors)
        else:
            return str


class StataReader(StataParser):
    """
    Class for working with a Stata dataset. There are two possibilities for
    usage:

     * The from_dta() method on the DataFrame class.
       This will return a DataFrame with the Stata dataset. Note that when
       using the from_dta() method, you will not have access to
       meta-information like variable labels or the data label.

     * Work with this object directly. Upon instantiation, the header of the
       Stata data file is read, giving you access to attributes like
       variable_labels(), data_label(), nobs(), ... A DataFrame with the data
       is returned by the read() method; this will also fill up the
       value_labels. Note that calling the value_labels() method will result in
       an error if the read() method has not been called yet. This is because
       the value labels are stored at the end of a Stata dataset, after the
       data.

    Parameters
    ----------
    path_or_buf : string or file-like object
        Path to .dta file or object implementing a binary read() functions
    encoding : string, None or encoding
        Encoding used to parse the files. Note that Stata doesn't
        support unicode. None defaults to cp1252.
    """

    def __init__(self, path_or_buf, encoding='cp1252'):
        super(StataReader, self).__init__(encoding)
        self.col_sizes = ()
        self._has_string_data = False
        self._missing_values = False
        self._data_read = False
        self._value_labels_read = False
        if isinstance(path_or_buf, str):
            path_or_buf, encoding = get_filepath_or_buffer(
                path_or_buf, encoding=self._default_encoding
            )

        if isinstance(path_or_buf, (str, compat.text_type, bytes)):
            self.path_or_buf = open(path_or_buf, 'rb')
        else:
            self.path_or_buf = path_or_buf

        self._read_header()

    def _read_header(self):
        first_char = self.path_or_buf.read(1)
        if struct.unpack('c', first_char)[0] == b'<':
            # format 117 or higher (XML like)
            self.path_or_buf.read(27)  # stata_dta><header><release>
            self.format_version = int(self.path_or_buf.read(3))
            if self.format_version not in [117]:
                raise ValueError("Version of given Stata file is not 104, "
                                 "105, 108, 113 (Stata 8/9), 114 (Stata "
                                 "10/11), 115 (Stata 12) or 117 (Stata 13)")
            self.path_or_buf.read(21)  # </release><byteorder>
            self.byteorder = self.path_or_buf.read(3) == "MSF" and '>' or '<'
            self.path_or_buf.read(15)  # </byteorder><K>
            self.nvar = struct.unpack(self.byteorder + 'H',
                                      self.path_or_buf.read(2))[0]
            self.path_or_buf.read(7)  # </K><N>
            self.nobs = struct.unpack(self.byteorder + 'I',
                                      self.path_or_buf.read(4))[0]
            self.path_or_buf.read(11)  # </N><label>
            strlen = struct.unpack('b', self.path_or_buf.read(1))[0]
            self.data_label = self._null_terminate(self.path_or_buf.read(strlen))
            self.path_or_buf.read(19)  # </label><timestamp>
            strlen = struct.unpack('b', self.path_or_buf.read(1))[0]
            self.time_stamp = self._null_terminate(self.path_or_buf.read(strlen))
            self.path_or_buf.read(26)  # </timestamp></header><map>
            self.path_or_buf.read(8)  # 0x0000000000000000
            self.path_or_buf.read(8)  # position of <map>
            seek_vartypes = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 16
            seek_varnames = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 10
            seek_sortlist = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 10
            seek_formats = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 9
            seek_value_label_names = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 19
            # Stata 117 data files do not follow the described format.  This is
            # a work around that uses the previous label, 33 bytes for each
            # variable, 20 for the closing tag and 17 for the opening tag
            self.path_or_buf.read(8)  # <variable_lables>, throw away
            seek_variable_labels = seek_value_label_names + (33*self.nvar) + 20 + 17
            # Below is the original, correct code (per Stata sta format doc,
            # although this is not followed in actual 117 dtas)
            #seek_variable_labels = struct.unpack(
            #    self.byteorder + 'q', self.path_or_buf.read(8))[0] + 17
            self.path_or_buf.read(8)  # <characteristics>
            self.data_location = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 6
            self.seek_strls = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 7
            self.seek_value_labels = struct.unpack(
                self.byteorder + 'q', self.path_or_buf.read(8))[0] + 14
            #self.path_or_buf.read(8)  # </stata_dta>
            #self.path_or_buf.read(8)  # EOF
            self.path_or_buf.seek(seek_vartypes)
            typlist = [struct.unpack(self.byteorder + 'H',
                                     self.path_or_buf.read(2))[0]
                       for i in range(self.nvar)]
            self.typlist = [None]*self.nvar
            try:
                i = 0
                for typ in typlist:
                    if typ <= 2045:
                        self.typlist[i] = typ
                    elif typ == 32768:
                        raise ValueError("Long strings are not supported")
                    else:
                        self.typlist[i] = self.TYPE_MAP_XML[typ]
                    i += 1
            except:
                raise ValueError("cannot convert stata types [{0}]"
                                 .format(','.join(typlist)))
            self.dtyplist = [None]*self.nvar
            try:
                i = 0
                for typ in typlist:
                    if typ <= 2045:
                        self.dtyplist[i] = str(typ)
                    else:
                        self.dtyplist[i] = self.DTYPE_MAP_XML[typ]
                    i += 1
            except:
                raise ValueError("cannot convert stata dtypes [{0}]"
                                 .format(','.join(typlist)))

            self.path_or_buf.seek(seek_varnames)
            self.varlist = [self._null_terminate(self.path_or_buf.read(33))
                            for i in range(self.nvar)]

            self.path_or_buf.seek(seek_sortlist)
            self.srtlist = struct.unpack(
                self.byteorder + ('h' * (self.nvar + 1)),
                self.path_or_buf.read(2 * (self.nvar + 1))
            )[:-1]

            self.path_or_buf.seek(seek_formats)
            self.fmtlist = [self._null_terminate(self.path_or_buf.read(49))
                            for i in range(self.nvar)]

            self.path_or_buf.seek(seek_value_label_names)
            self.lbllist = [self._null_terminate(self.path_or_buf.read(33))
                            for i in range(self.nvar)]

            self.path_or_buf.seek(seek_variable_labels)
            self.vlblist = [self._null_terminate(self.path_or_buf.read(81))
                            for i in range(self.nvar)]
        else:
            # header
            self.format_version = struct.unpack('b', first_char)[0]
            if self.format_version not in [104, 105, 108, 113, 114, 115]:
                raise ValueError("Version of given Stata file is not 104, "
                                 "105, 108, 113 (Stata 8/9), 114 (Stata "
                                 "10/11), 115 (Stata 12) or 117 (Stata 13)")
            self.byteorder = struct.unpack('b', self.path_or_buf.read(1))[0] == 0x1 and '>' or '<'
            self.filetype = struct.unpack('b', self.path_or_buf.read(1))[0]
            self.path_or_buf.read(1)  # unused

            self.nvar = struct.unpack(self.byteorder + 'H',
                                      self.path_or_buf.read(2))[0]
            self.nobs = struct.unpack(self.byteorder + 'I',
                                      self.path_or_buf.read(4))[0]
            if self.format_version > 105:
                self.data_label = self._null_terminate(self.path_or_buf.read(81))
            else:
                self.data_label = self._null_terminate(self.path_or_buf.read(32))
            if self.format_version > 104:
                self.time_stamp = self._null_terminate(self.path_or_buf.read(18))

            # descriptors
            if self.format_version > 108:
                typlist = [ord(self.path_or_buf.read(1))
                           for i in range(self.nvar)]
            else:
                typlist = [
                    self.OLD_TYPE_MAPPING[
                        self._decode_bytes(self.path_or_buf.read(1))
                    ] for i in range(self.nvar)
                ]

            try:
                self.typlist = [self.TYPE_MAP[typ] for typ in typlist]
            except:
                raise ValueError("cannot convert stata types [{0}]"
                                 .format(','.join(typlist)))
            try:
                self.dtyplist = [self.DTYPE_MAP[typ] for typ in typlist]
            except:
                raise ValueError("cannot convert stata dtypes [{0}]"
                                 .format(','.join(typlist)))

            if self.format_version > 108:
                self.varlist = [self._null_terminate(self.path_or_buf.read(33))
                                for i in range(self.nvar)]
            else:
                self.varlist = [self._null_terminate(self.path_or_buf.read(9))
                                for i in range(self.nvar)]
            self.srtlist = struct.unpack(
                self.byteorder + ('h' * (self.nvar + 1)),
                self.path_or_buf.read(2 * (self.nvar + 1))
            )[:-1]
            if self.format_version > 113:
                self.fmtlist = [self._null_terminate(self.path_or_buf.read(49))
                                for i in range(self.nvar)]
            elif self.format_version > 104:
                self.fmtlist = [self._null_terminate(self.path_or_buf.read(12))
                                for i in range(self.nvar)]
            else:
                self.fmtlist = [self._null_terminate(self.path_or_buf.read(7))
                                for i in range(self.nvar)]
            if self.format_version > 108:
                self.lbllist = [self._null_terminate(self.path_or_buf.read(33))
                                for i in range(self.nvar)]
            else:
                self.lbllist = [self._null_terminate(self.path_or_buf.read(9))
                                for i in range(self.nvar)]
            if self.format_version > 105:
                self.vlblist = [self._null_terminate(self.path_or_buf.read(81))
                                for i in range(self.nvar)]
            else:
                self.vlblist = [self._null_terminate(self.path_or_buf.read(32))
                                for i in range(self.nvar)]

            # ignore expansion fields (Format 105 and later)
            # When reading, read five bytes; the last four bytes now tell you
            # the size of the next read, which you discard.  You then continue
            # like this until you read 5 bytes of zeros.

            if self.format_version > 104:
                while True:
                    data_type = struct.unpack(self.byteorder + 'b',
                                              self.path_or_buf.read(1))[0]
                    if self.format_version > 108:
                        data_len = struct.unpack(self.byteorder + 'i',
                                                 self.path_or_buf.read(4))[0]
                    else:
                        data_len = struct.unpack(self.byteorder + 'h',
                                                 self.path_or_buf.read(2))[0]
                    if data_type == 0:
                        break
                    self.path_or_buf.read(data_len)

            # necessary data to continue parsing
            self.data_location = self.path_or_buf.tell()

        self.has_string_data = len([x for x in self.typlist
                                    if type(x) is int]) > 0

        """Calculate size of a data record."""
        self.col_sizes = lmap(lambda x: self._calcsize(x), self.typlist)

    def _calcsize(self, fmt):
        return (type(fmt) is int and fmt
                or struct.calcsize(self.byteorder + fmt))

    def _null_terminate(self, s):
        if compat.PY3 or self._encoding is not None:  # have bytes not strings,
                                                      # so must decode
            null_byte = b"\0"
            try:
                s = s[:s.index(null_byte)]
            except:
                pass
            return s.decode(self._encoding or self._default_encoding)
        else:
            null_byte = "\0"
            try:
                return s.lstrip(null_byte)[:s.index(null_byte)]
            except:
                return s

    def _read_value_labels(self):
        if self.format_version >= 117:
            self.path_or_buf.seek(self.seek_value_labels)
        else:
            if not self._data_read:
                raise Exception("Data has not been read. Because of the "
                                "layout of Stata files, this is necessary "
                                "before reading value labels.")
            if self._value_labels_read:
                raise Exception("Value labels have already been read.")

        self.value_label_dict = dict()

        if self.format_version <= 108:
            # Value labels are not supported in version 108 and earlier.
            return

        while True:
            if self.format_version >= 117:
                if self.path_or_buf.read(5) == b'</val':  # <lbl>
                    break  # end o f variable lable table

            slength = self.path_or_buf.read(4)
            if not slength:
                break  # end of variable lable table (format < 117)
            labname = self._null_terminate(self.path_or_buf.read(33))
            self.path_or_buf.read(3)  # padding

            n = struct.unpack(self.byteorder + 'I',
                              self.path_or_buf.read(4))[0]
            txtlen = struct.unpack(self.byteorder + 'I',
                                   self.path_or_buf.read(4))[0]
            off = []
            for i in range(n):
                off.append(struct.unpack(self.byteorder + 'I',
                                         self.path_or_buf.read(4))[0])
            val = []
            for i in range(n):
                val.append(struct.unpack(self.byteorder + 'I',
                                         self.path_or_buf.read(4))[0])
            txt = self.path_or_buf.read(txtlen)
            self.value_label_dict[labname] = dict()
            for i in range(n):
                self.value_label_dict[labname][val[i]] = (
                    self._null_terminate(txt[off[i]:])
                )

            if self.format_version >= 117:
                self.path_or_buf.read(6)  # </lbl>
        self._value_labels_read = True

    def _read_strls(self):
        self.path_or_buf.seek(self.seek_strls)
        self.GSO = dict()
        while True:
            if self.path_or_buf.read(3) != b'GSO':
                break

            v_o = struct.unpack(self.byteorder + 'L',
                                self.path_or_buf.read(8))[0]
            typ = self.path_or_buf.read(1)
            length = struct.unpack(self.byteorder + 'I',
                                   self.path_or_buf.read(4))[0]
            self.GSO[v_o] = self.path_or_buf.read(length-1)
            self.path_or_buf.read(1)  # zero-termination

    def data(self, convert_dates=True, convert_categoricals=True, index=None,
             convert_missing=False):
        """
        Reads observations from Stata file, converting them into a dataframe

        Parameters
        ----------
        convert_dates : boolean, defaults to True
            Convert date variables to DataFrame time values
        convert_categoricals : boolean, defaults to True
            Read value labels and convert columns to Categorical/Factor
            variables
        index : identifier of index column
            identifier of column that should be used as index of the DataFrame
        convert_missing : boolean, defaults to False
            Flag indicating whether to convert missing values to their Stata
            representation.  If False, missing values are replaced with
            nans.  If True, columns containing missing values are returned with
            object data types and missing values are represented by
            StataMissingValue objects.

        Returns
        -------
        y : DataFrame instance
        """
        self._missing_values = convert_missing
        if self._data_read:
            raise Exception("Data has already been read.")
        self._data_read = True

        if self.format_version >= 117:
            self._read_strls()

        # Read data
        count = self.nobs
        dtype = []  # Convert struct data types to numpy data type
        for i, typ in enumerate(self.typlist):
            if typ in self.NUMPY_TYPE_MAP:
                dtype.append(('s' + str(i), self.NUMPY_TYPE_MAP[typ]))
            else:
                dtype.append(('s' + str(i), 'S' + str(typ)))
        dtype = np.dtype(dtype)
        read_len = count * dtype.itemsize
        self.path_or_buf.seek(self.data_location)
        data = np.frombuffer(self.path_or_buf.read(read_len),dtype=dtype,count=count)
        self._data_read = True

        if convert_categoricals:
            self._read_value_labels()

        if len(data)==0:
            data = DataFrame(columns=self.varlist, index=index)
        else:
            data = DataFrame.from_records(data, index=index)
            data.columns = self.varlist

        for col, typ in zip(data, self.typlist):
            if type(typ) is int:
                data[col] = data[col].apply(self._null_terminate, convert_dtype=True,)

        cols_ = np.where(self.dtyplist)[0]

        # Convert columns (if needed) to match input type
        index = data.index
        requires_type_conversion = False
        data_formatted = []
        for i in cols_:
            if self.dtyplist[i] is not None:
                col = data.columns[i]
                dtype = data[col].dtype
                if (dtype != np.dtype(object)) and (dtype != self.dtyplist[i]):
                    requires_type_conversion = True
                    data_formatted.append((col, Series(data[col], index, self.dtyplist[i])))
                else:
                    data_formatted.append((col, data[col]))
        if requires_type_conversion:
            data = DataFrame.from_items(data_formatted)
        del data_formatted

        # Check for missing values, and replace if found
        for i, colname in enumerate(data):
            fmt = self.typlist[i]
            if fmt not in self.VALID_RANGE:
                continue

            nmin, nmax = self.VALID_RANGE[fmt]
            series = data[colname]
            missing = np.logical_or(series < nmin, series > nmax)

            if not missing.any():
                continue

            if self._missing_values:  # Replacement follows Stata notation
                missing_loc = np.argwhere(missing)
                umissing, umissing_loc = np.unique(series[missing],
                                                   return_inverse=True)
                replacement = Series(series, dtype=np.object)
                for i, um in enumerate(umissing):
                    missing_value = StataMissingValue(um)

                    loc = missing_loc[umissing_loc == i]
                    replacement.iloc[loc] = missing_value
            else:  # All replacements are identical
                dtype = series.dtype
                if dtype not in (np.float32, np.float64):
                    dtype = np.float64
                replacement = Series(series, dtype=dtype)
                replacement[missing] = np.nan

            data[colname] = replacement

        if convert_dates:
            cols = np.where(lmap(lambda x: x in _date_formats,
                                 self.fmtlist))[0]
            for i in cols:
                col = data.columns[i]
                data[col] = _stata_elapsed_date_to_datetime_vec(data[col], self.fmtlist[i])

        if convert_categoricals:
            cols = np.where(
                lmap(lambda x: x in compat.iterkeys(self.value_label_dict),
                     self.lbllist)
            )[0]
            for i in cols:
                col = data.columns[i]
                labeled_data = np.copy(data[col])
                labeled_data = labeled_data.astype(object)
                for k, v in compat.iteritems(
                        self.value_label_dict[self.lbllist[i]]):
                    labeled_data[(data[col] == k).values] = v
                data[col] = Categorical.from_array(labeled_data)

        return data

    def data_label(self):
        """Returns data label of Stata file"""
        return self.data_label

    def variable_labels(self):
        """Returns variable labels as a dict, associating each variable name
        with corresponding label
        """
        return dict(zip(self.varlist, self.vlblist))

    def value_labels(self):
        """Returns a dict, associating each variable name a dict, associating
        each value its corresponding label
        """
        if not self._value_labels_read:
            self._read_value_labels()

        return self.value_label_dict


def _open_file_binary_write(fname, encoding):
    if hasattr(fname, 'write'):
        #if 'b' not in fname.mode:
        return fname
    return open(fname, "wb")


def _set_endianness(endianness):
    if endianness.lower() in ["<", "little"]:
        return "<"
    elif endianness.lower() in [">", "big"]:
        return ">"
    else:  # pragma : no cover
        raise ValueError("Endianness %s not understood" % endianness)


def _pad_bytes(name, length):
    """
    Takes a char string and pads it wih null bytes until it's length chars
    """
    return name + "\x00" * (length - len(name))


def _default_names(nvar):
    """
    Returns default Stata names v1, v2, ... vnvar
    """
    return ["v%d" % i for i in range(1, nvar+1)]


def _convert_datetime_to_stata_type(fmt):
    """
    Converts from one of the stata date formats to a type in TYPE_MAP
    """
    if fmt in ["tc", "%tc", "td", "%td", "tw", "%tw", "tm", "%tm", "tq",
               "%tq", "th", "%th", "ty", "%ty"]:
        return np.float64  # Stata expects doubles for SIFs
    else:
        raise ValueError("fmt %s not understood" % fmt)


def _maybe_convert_to_int_keys(convert_dates, varlist):
    new_dict = {}
    for key in convert_dates:
        if not convert_dates[key].startswith("%"):  # make sure proper fmts
            convert_dates[key] = "%" + convert_dates[key]
        if key in varlist:
            new_dict.update({varlist.index(key): convert_dates[key]})
        else:
            if not isinstance(key, int):
                raise ValueError(
                    "convert_dates key is not in varlist and is not an int"
                )
            new_dict.update({key: convert_dates[key]})
    return new_dict


def _dtype_to_stata_type(dtype):
    """
    Converts dtype types to stata types. Returns the byte of the given ordinal.
    See TYPE_MAP and comments for an explanation. This is also explained in
    the dta spec.
    1 - 244 are strings of this length
                         Pandas    Stata
    251 - chr(251) - for int8      byte
    252 - chr(252) - for int16     int
    253 - chr(253) - for int32     long
    254 - chr(254) - for float32   float
    255 - chr(255) - for double    double

    If there are dates to convert, then dtype will already have the correct
    type inserted.
    """
    #TODO: expand to handle datetime to integer conversion
    if dtype.type == np.string_:
        return chr(dtype.itemsize)
    elif dtype.type == np.object_:  # try to coerce it to the biggest string
                                    # not memory efficient, what else could we
                                    # do?
        return chr(244)
    elif dtype == np.float64:
        return chr(255)
    elif dtype == np.float32:
        return chr(254)
    elif dtype == np.int32:
        return chr(253)
    elif dtype == np.int16:
        return chr(252)
    elif dtype == np.int8:
        return chr(251)
    else:  # pragma : no cover
        raise ValueError("Data type %s not currently understood. "
                         "Please report an error to the developers." % dtype)


def _dtype_to_default_stata_fmt(dtype, column):
    """
    Maps numpy dtype to stata's default format for this type. Not terribly
    important since users can change this in Stata. Semantics are

    object  -> "%DDs" where DD is the length of the string.  If not a string,
                raise ValueError
    float64 -> "%10.0g"
    float32 -> "%9.0g"
    int64   -> "%9.0g"
    int32   -> "%12.0g"
    int16   -> "%8.0g"
    int8    -> "%8.0g"
    """
    # TODO: expand this to handle a default datetime format?
    if dtype.type == np.object_:
        inferred_dtype = infer_dtype(column.dropna())
        if not (inferred_dtype in ('string', 'unicode')
                or len(column) == 0):
            raise ValueError('Writing general object arrays is not supported')
        itemsize = max_len_string_array(column.values)
        if itemsize > 244:
            raise ValueError(excessive_string_length_error % column.name)

        return "%" + str(itemsize) + "s"
    elif dtype == np.float64:
        return "%10.0g"
    elif dtype == np.float32:
        return "%9.0g"
    elif dtype == np.int32:
        return "%12.0g"
    elif dtype == np.int8 or dtype == np.int16:
        return "%8.0g"
    else:  # pragma : no cover
        raise ValueError("Data type %s not currently understood. "
                         "Please report an error to the developers." % dtype)


class StataWriter(StataParser):
    """
    A class for writing Stata binary dta files from array-like objects

    Parameters
    ----------
    fname : file path or buffer
        Where to save the dta file.
    data : array-like
        Array-like input to save. Pandas objects are also accepted.
    convert_dates : dict
        Dictionary mapping column of datetime types to the stata internal
        format that you want to use for the dates. Options are
        'tc', 'td', 'tm', 'tw', 'th', 'tq', 'ty'. Column can be either a
        number or a name.
    encoding : str
        Default is latin-1. Note that Stata does not support unicode.
    byteorder : str
        Can be ">", "<", "little", or "big". The default is None which uses
        `sys.byteorder`
    time_stamp : datetime
        A date time to use when writing the file.  Can be None, in which
        case the current time is used.
    dataset_label : str
        A label for the data set.  Should be 80 characters or smaller.

    Returns
    -------
    writer : StataWriter instance
        The StataWriter instance has a write_file method, which will
        write the file to the given `fname`.

    Examples
    --------
    >>> import pandas as pd
    >>> data = pd.DataFrame([[1.0, 1]], columns=['a', 'b'])
    >>> writer = StataWriter('./data_file.dta', data)
    >>> writer.write_file()

    Or with dates
    >>> from datetime import datetime
    >>> data = pd.DataFrame([[datetime(2000,1,1)]], columns=['date'])
    >>> writer = StataWriter('./date_data_file.dta', data, {'date' : 'tw'})
    >>> writer.write_file()
    """
    def __init__(self, fname, data, convert_dates=None, write_index=True,
                 encoding="latin-1", byteorder=None, time_stamp=None,
                 data_label=None):
        super(StataWriter, self).__init__(encoding)
        self._convert_dates = convert_dates
        self._write_index = write_index
        self._time_stamp = time_stamp
        self._data_label = data_label
        # attach nobs, nvars, data, varlist, typlist
        self._prepare_pandas(data)

        if byteorder is None:
            byteorder = sys.byteorder
        self._byteorder = _set_endianness(byteorder)
        self._file = _open_file_binary_write(
            fname, self._encoding or self._default_encoding
        )
        self.type_converters = {253: np.int32, 252: np.int16, 251: np.int8}

    def _write(self, to_write):
        """
        Helper to call encode before writing to file for Python 3 compat.
        """
        if compat.PY3:
            self._file.write(to_write.encode(self._encoding or
                                             self._default_encoding))
        else:
            self._file.write(to_write)


    def _replace_nans(self, data):
        # return data
        """Checks floating point data columns for nans, and replaces these with
        the generic Stata for missing value (.)"""
        for c in data:
            dtype = data[c].dtype
            if dtype in (np.float32, np.float64):
                if dtype == np.float32:
                    replacement = self.MISSING_VALUES['f']
                else:
                    replacement = self.MISSING_VALUES['d']
                data[c] = data[c].fillna(replacement)

        return data

    def _check_column_names(self, data):
        """Checks column names to ensure that they are valid Stata column names.
        This includes checks for:
            * Non-string names
            * Stata keywords
            * Variables that start with numbers
            * Variables with names that are too long

        When an illegal variable name is detected, it is converted, and if dates
        are exported, the variable name is propogated to the date conversion
        dictionary
        """
        converted_names = []
        columns = list(data.columns)
        original_columns = columns[:]

        duplicate_var_id = 0
        for j, name in enumerate(columns):
            orig_name = name
            if not isinstance(name, string_types):
                name = text_type(name)

            for c in name:
                if (c < 'A' or c > 'Z') and (c < 'a' or c > 'z') and \
                        (c < '0' or c > '9') and c != '_':
                    name = name.replace(c, '_')

            # Variable name must not be a reserved word
            if name in self.RESERVED_WORDS:
                name = '_' + name

            # Variable name may not start with a number
            if name[0] >= '0' and name[0] <= '9':
                name = '_' + name

            name = name[:min(len(name), 32)]

            if not name == orig_name:
                # check for duplicates
                while columns.count(name) > 0:
                    # prepend ascending number to avoid duplicates
                    name = '_' + str(duplicate_var_id) + name
                    name = name[:min(len(name), 32)]
                    duplicate_var_id += 1

                # need to possibly encode the orig name if its unicode
                try:
                    orig_name = orig_name.encode('utf-8')
                except:
                    pass
                converted_names.append('{0}   ->   {1}'.format(orig_name, name))

            columns[j] = name

        data.columns = columns

        # Check date conversion, and fix key if needed
        if self._convert_dates:
            for c, o in zip(columns, original_columns):
                if c != o:
                    self._convert_dates[c] = self._convert_dates[o]
                    del self._convert_dates[o]

        if converted_names:
            import warnings

            ws = invalid_name_doc.format('\n    '.join(converted_names))
            warnings.warn(ws, InvalidColumnName)

        return data

    def _prepare_pandas(self, data):
        #NOTE: we might need a different API / class for pandas objects so
        # we can set different semantics - handle this with a PR to pandas.io
        class DataFrameRowIter(object):
            def __init__(self, data):
                self.data = data

            def __iter__(self):
                for row in data.itertuples():
                    # First element is index, so remove
                    yield row[1:]

        if self._write_index:
            data = data.reset_index()
        # Check columns for compatibility with stata
        data = _cast_to_stata_types(data)
        # Ensure column names are strings
        data = self._check_column_names(data)
        # Replace NaNs with Stata missing values
        data = self._replace_nans(data)
        self.datarows = DataFrameRowIter(data)
        self.nobs, self.nvar = data.shape
        self.data = data
        self.varlist = data.columns.tolist()
        dtypes = data.dtypes
        if self._convert_dates is not None:
            self._convert_dates = _maybe_convert_to_int_keys(
                self._convert_dates, self.varlist
            )
            for key in self._convert_dates:
                new_type = _convert_datetime_to_stata_type(
                    self._convert_dates[key]
                )
                dtypes[key] = np.dtype(new_type)
        self.typlist = [_dtype_to_stata_type(dt) for dt in dtypes]
        self.fmtlist = []
        for col, dtype in dtypes.iteritems():
            self.fmtlist.append(_dtype_to_default_stata_fmt(dtype, data[col]))
        # set the given format for the datetime cols
        if self._convert_dates is not None:
            for key in self._convert_dates:
                self.fmtlist[key] = self._convert_dates[key]

    def write_file(self):
        self._write_header(time_stamp=self._time_stamp,
                           data_label=self._data_label)
        self._write_descriptors()
        self._write_variable_labels()
        # write 5 zeros for expansion fields
        self._write(_pad_bytes("", 5))
        self._prepare_data()
        self._write_data()
        self._file.close()

    def _write_header(self, data_label=None, time_stamp=None):
        byteorder = self._byteorder
        # ds_format - just use 114
        self._file.write(struct.pack("b", 114))
        # byteorder
        self._write(byteorder == ">" and "\x01" or "\x02")
        # filetype
        self._write("\x01")
        # unused
        self._write("\x00")
        # number of vars, 2 bytes
        self._file.write(struct.pack(byteorder+"h", self.nvar)[:2])
        # number of obs, 4 bytes
        self._file.write(struct.pack(byteorder+"i", self.nobs)[:4])
        # data label 81 bytes, char, null terminated
        if data_label is None:
            self._file.write(self._null_terminate(_pad_bytes("", 80)))
        else:
            self._file.write(
                self._null_terminate(_pad_bytes(data_label[:80], 80))
            )
        # time stamp, 18 bytes, char, null terminated
        # format dd Mon yyyy hh:mm
        if time_stamp is None:
            time_stamp = datetime.datetime.now()
        elif not isinstance(time_stamp, datetime.datetime):
            raise ValueError("time_stamp should be datetime type")
        self._file.write(
            self._null_terminate(time_stamp.strftime("%d %b %Y %H:%M"))
        )

    def _write_descriptors(self, typlist=None, varlist=None, srtlist=None,
                           fmtlist=None, lbllist=None):
        nvar = self.nvar
        # typlist, length nvar, format byte array
        for typ in self.typlist:
            self._write(typ)

        # varlist names are checked by _check_column_names
        # varlist, requires null terminated
        for name in self.varlist:
            name = self._null_terminate(name, True)
            name = _pad_bytes(name[:32], 33)
            self._write(name)

        # srtlist, 2*(nvar+1), int array, encoded by byteorder
        srtlist = _pad_bytes("", (2*(nvar+1)))
        self._write(srtlist)

        # fmtlist, 49*nvar, char array
        for fmt in self.fmtlist:
            self._write(_pad_bytes(fmt, 49))

        # lbllist, 33*nvar, char array
        #NOTE: this is where you could get fancy with pandas categorical type
        for i in range(nvar):
            self._write(_pad_bytes("", 33))

    def _write_variable_labels(self, labels=None):
        nvar = self.nvar
        if labels is None:
            for i in range(nvar):
                self._write(_pad_bytes("", 81))

    def _prepare_data(self):
        data = self.data.copy()
        typlist = self.typlist
        convert_dates = self._convert_dates
        # 1. Convert dates
        if self._convert_dates is not None:
            for i, col in enumerate(data):
                if i in convert_dates:
                    data[col] = _datetime_to_stata_elapsed_vec(data[col],
                                                               self.fmtlist[i])

        # 2. Convert bad string data to '' and pad to correct length
        dtype = []
        data_cols = []
        has_strings = False
        for i, col in enumerate(data):
            typ = ord(typlist[i])
            if typ <= 244:
                has_strings = True
                data[col] = data[col].fillna('').apply(_pad_bytes, args=(typ,))
                stype = 'S%d' % typ
                dtype.append(('c'+str(i), stype))
                string = data[col].str.encode(self._encoding)
                data_cols.append(string.values.astype(stype))
            else:
                dtype.append(('c'+str(i), data[col].dtype))
                data_cols.append(data[col].values)
        dtype = np.dtype(dtype)

        # 3. Convert to record array

        # data.to_records(index=False, convert_datetime64=False)
        if has_strings:
            self.data = np.fromiter(zip(*data_cols), dtype=dtype)
        else:
            self.data = data.to_records(index=False)

    def _write_data(self):
        data = self.data
        data.tofile(self._file)

    def _null_terminate(self, s, as_string=False):
        null_byte = '\x00'
        if compat.PY3 and not as_string:
            s += null_byte
            return s.encode(self._encoding)
        else:
            s += null_byte
            return s
