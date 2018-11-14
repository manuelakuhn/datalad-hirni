"""Convert DICOM data to BIDS based on the respective study specification"""

__docformat__ = 'restructuredtext'

import os.path as op
from os.path import isabs
from os.path import join as opj
from os.path import basename
from os.path import lexists
from os.path import relpath

from datalad.interface.base import Interface
from datalad.interface.base import build_doc
from datalad.support.param import Parameter
from datalad.distribution.dataset import datasetmethod
from datalad.distribution.dataset import EnsureDataset
from datalad.distribution.dataset import require_dataset
from datalad.distribution.dataset import resolve_path
from datalad.interface.results import get_status_dict
from datalad.interface.utils import eval_results
from datalad.support.constraints import EnsureStr
from datalad.support.constraints import EnsureNone
from datalad.support.exceptions import InsufficientArgumentsError
from datalad.support.json_py import load_stream
from datalad.utils import assure_list
from datalad.utils import rmtree

from datalad.coreapi import remove
from datalad_container import containers_run
import datalad_hirni.support.hirni_heuristic as heuristic
import logging

lgr = logging.getLogger("datalad.hirni.spec2bids")


@build_doc
class Spec2Bids(Interface):
    """Convert to BIDS based on study specification
    """

    _params_ = dict(
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""bids dataset""",
            constraints=EnsureDataset() | EnsureNone()),
        specfile=Parameter(
            args=("specfile",),
            metavar="SPEC_FILE",
            doc="""path(s) to the specification file(s) to use for conversion.
             If a directory at the first level beneath the dataset's root is 
             given instead of a file, it's assumed to be an acqusition directory 
             that contains a specification file.
             By default this is a file named 'studyspec.json' in the
             acquisition directory. This default name can be configured via the
             'datalad.hirni.studyspec.filename' config variable.
             """,
            nargs="*",
            constraints=EnsureStr()),
        anonymize=Parameter(
            args=("--anonymize",),
            action="store_true",
            doc="""whether or not to anonymize for conversion. By now this means
            to use 'anon_subject' instead of 'subject' from spec and to use 
            datalad-run with a sidecar file, to not leak potentially identifying 
            information into its record."""
        )
    )

    @staticmethod
    @datasetmethod(name='hirni_spec2bids')
    @eval_results
    def __call__(specfile, dataset=None, anonymize=False):

        dataset = require_dataset(dataset, check_installed=True,
                                  purpose="spec2bids")

        specfile = assure_list(specfile)
        specfile = [resolve_path(p, dataset) for p in specfile]

        for spec_path in specfile:
            if not lexists(spec_path):
                yield get_status_dict(
                    action='spec2bids',
                    path=spec_path,
                    status='impossible',
                    message="{} not found".format(spec_path)
                )

            if op.isdir(spec_path):
                if op.realpath(op.join(spec_path, op.pardir)) == \
                        op.realpath(dataset.path):
                    spec_path = op.join(
                            spec_path,
                            dataset.config.get(
                                    "datalad.hirni.studyspec.filename",
                                    "studyspec.json")
                    )
                    # TODO: check existence of that file!
                else:
                    yield get_status_dict(
                        action='spec2bids',
                        path=spec_path,
                        status='impossible',
                        message="{} is neither a specification file nor an "
                                "acquisition directory".format(spec_path)
                    )

            ran_procedure = dict()

            # relative path to spec to be recorded:
            rel_spec_path = relpath(spec_path, dataset.path) \
                if isabs(spec_path) else spec_path

            # check each dict (snippet) in the specification for what to do
            # wrt conversion:
            for spec_snippet in load_stream(spec_path):

                # TODO: value 'ignore'!?

                if 'procedures' not in spec_snippet:
                    # no conversion procedures defined at all:
                    yield get_status_dict(
                            action='spec2bids',
                            path=spec_path,
                            snippet=spec_snippet,
                            status='notneeded',
                    )
                    continue

                procedure_list = spec_snippet['procedures']
                if not procedure_list:
                    # no conversion procedures defined at all:
                    yield get_status_dict(
                            action='spec2bids',
                            path=spec_path,
                            snippet=spec_snippet,
                            status='notneeded',
                    )
                    continue

                # accept a single dict as a one item list:
                if isinstance(procedure_list, dict):
                    procedure_list = [procedure_list]

                # build a dict available for placeholders in format strings:
                # Note: This is flattening the structure since we don't need
                # value/approved for the substitutions. In addition 'subject'
                # and 'anon_subject' are not passed on, but a new key
                # 'bids_subject' instead the value of which depends on the
                # --anonymize switch.
                # Additionally 'location' is recomputed to be relative to
                # dataset.path, since this is where the procedures are running
                # from within.
                replacements = dict()
                for k, v in spec_snippet.items():
                    if k == 'subject':
                        if not anonymize:
                            replacements['bids-subject'] = v['value']
                    elif k == 'anon-subject':
                        if anonymize:
                            replacements['bids-subject'] = v['value']
                    elif k == 'location':
                        replacements[k] = op.join(op.dirname(rel_spec_path), v)
                    elif k == 'procedures':
                        # 'procedures' is a list of dicts (not suitable for
                        # substitutions) and it makes little sense to be
                        # referenced by converter format strings anyway:
                        continue
                    else:
                        replacements[k] = v['value'] if isinstance(v, dict) else v

                # build dict to patch os.environ with for passing
                # replacements on to procedures:
                env_subs = dict()
                for k, v in replacements.items():
                    env_subs['DATALAD_RUN_SUBSTITUTIONS_{}'
                             ''.format(k.upper().replace('-', '__'))] = str(v)
                env_subs['DATALAD_RUN_SUBSTITUTIONS_SPECPATH'] = rel_spec_path
                env_subs['DATALAD_RUN_SUBSTITUTIONS_ANONYMIZE'] = str(anonymize)

                # TODO: The above two blocks to build replacements dict and
                # env_subs should be joined eventually.

                for proc in procedure_list:
                    if heuristic.has_specval(proc, 'procedure-name'):
                        proc_name = heuristic.get_specval(proc, 'procedure-name')
                    else:
                        # invalid procedure spec
                        lgr.warning("conversion procedure missing key "
                                    "'procedure-name' in %s: %s",
                                    spec_path, proc)
                        # TODO: continue or yield impossible/error so it can be
                        # dealt with via on_failure?
                        continue

                    if proc_name == 'ignore':
                        continue

                    proc_call = heuristic.get_specval(proc, 'procedure-call') \
                        if heuristic.has_specval(proc, 'procedure-call') \
                        else None

                    only_once = heuristic.get_specval(proc,
                                                      'once-per-acquisition') \
                        if heuristic.has_specval(proc, 'once-per-acquisition') \
                        else None

                    if only_once and ran_procedure.get(proc_name, False):
                        # if it wants to run only once per acquisition and we
                        # already ran it, don't call it again
                        continue

                    # if spec comes with call format string, it takes precedence
                    # over what is generally configured for the procedure
                    # TODO: Not sure yet whether this is how we should deal with it
                    if proc_call:
                        env_subs['DATALAD.PROCEDURES.{}.CALL-FORMAT'
                                 ''.format(proc_name)] = proc_call

                    run_results = list()
                    # Note, that we can't use dataset.config.overrides to
                    # pass run-substitution config to procedures, since we
                    # leave python context and thereby loose the dataset
                    # instance. Use patched os.environ instead. Note also,
                    # that this requires names of substitutions to not
                    # contain underscores, since they would be translated to
                    # '.' by ConfigManager when reading them from within the
                    # procedure's datalad-run calls.
                    from mock import patch

                    # TODO: Reconsider that patching. Shouldn't it be an update?
                    with patch.dict('os.environ', env_subs):
                        # apparently reload is necessary to consider config
                        # overrides via env:
                        dataset.config.reload()
                        for r in dataset.run_procedure(
                                spec='hirni-dicom-converter',
                                return_type='generator'
                        ):

                            # # if there was an issue yield original result,
                            # # otherwise swallow:
                            # if r['status'] not in ['ok', 'notneeded']:
                            yield r
                            run_results.append(r)

                    if not all(r['status'] in ['ok', 'notneeded']
                               for r in run_results):
                        yield {'action': proc_name,
                               'path': spec_path,
                               'snippet': spec_snippet,
                               'status': 'error',
                               'message': "acquisition conversion failed. "
                                          "See previous message(s)."}

                    else:
                        yield {'action': proc_name,
                               'path': spec_path,
                               'snippet': spec_snippet,
                               'status': 'ok',
                               'message': "acquisition converted."}

                    # mark as a procedure we ran on this acquisition:
                    ran_procedure[proc_name] = True

                    # elif proc_name != 'hirni-dicom-converter':
                    #     # specific converter procedure call
                    #
                    #     from mock import patch
                    #     with patch.dict('os.environ', env_subs):
                    #         # apparently reload is necessary to consider config
                    #         # overrides via env:
                    #         dataset.config.reload()
                    #
                    #         for r in dataset.run_procedure(
                    #                 spec=[proc_name, rel_spec_path, anonymize],
                    #                 return_type='generator'
                    #         ):
                    #
                    #             # if there was an issue with containers-run,
                    #             # yield original result, otherwise swallow:
                    #             if r['status'] not in ['ok', 'notneeded']:
                    #                 yield r
                    #
                    #             run_results.append(r)
                    #
                    #     if not all(r['status'] in ['ok', 'notneeded']
                    #                for r in run_results):
                    #         yield {'action': proc_name,
                    #                'path': spec_path,
                    #                'snippet': spec_snippet,
                    #                'status': 'error',
                    #                'message': "Conversion failed. "
                    #                           "See previous message(s)."}
                    #
                    #     else:
                    #         yield {'action': proc_name,
                    #                'path': spec_path,
                    #                'snippet': spec_snippet,
                    #                'status': 'ok',
                    #                'message': "specification converted."}

                    # elif ran_heudiconv and proc_name == 'hirni-dicom-converter':
                    #     # in this case we acted upon this snippet already and
                    #     # do not have to produce a result
                    #     pass
                    #
                    # else:
                    #     # this shouldn't happen!
                    #     raise RuntimeError

            yield {'action': 'spec2bids',
                   'path': spec_path,
                   'status': 'ok'}
