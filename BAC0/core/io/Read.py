
#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 by Christian Tremblay, P.Eng <christian.tremblay@servisys.com>
# Licensed under LGPLv3, see file LICENSE in this source tree.
#
'''
Read.py - creation of ReadProperty and ReadPropertyMultiple requests

    Used while defining an app:
    Example::

        class BasicScript(WhoisIAm, ReadProperty)

    Class::

        ReadProperty()
            def read()
            def readMultiple()

'''

#--- standard Python modules ---

#--- 3rd party modules ---
from bacpypes.debugging import bacpypes_debugging

from bacpypes.pdu import Address
from bacpypes.object import get_object_class, get_datatype
from bacpypes.apdu import PropertyReference, ReadAccessSpecification, \
    ReadPropertyRequest, ReadPropertyMultipleRequest, RejectReason, AbortReason, RejectPDU, AbortPDU

from bacpypes.basetypes import PropertyIdentifier
from bacpypes.apdu import ReadPropertyMultipleACK, ReadPropertyACK
from bacpypes.primitivedata import Unsigned
from bacpypes.constructeddata import Array
from bacpypes.iocb import IOCB
from bacpypes.core import deferred

#--- this application's modules ---
from .IOExceptions import ReadPropertyException, ReadPropertyMultipleException, NoResponseFromController, ApplicationNotStarted, UnrecognizedService, SegmentationNotSupported, UnknownPropertyError, UnknownObjectError

from ..utils.notes import note_and_log
#------------------------------------------------------------------------------


@note_and_log
class ReadProperty():
    """
    Defines BACnet Read functions: readProperty and readPropertyMultiple.
    Data exchange is made via a Queue object
    A timeout of 10 seconds allows detection of invalid device or communciation errors.
    """

    def read(self, args, arr_index=None, vendor_id=0, bacoid=None):
        """
        Build a ReadProperty request, wait for the answer and return the value

        :param args: String with <addr> <type> <inst> <prop> [ <indx> ]
        :returns: data read from device (str representing data like 10 or True)

        *Example*::

            import BAC0
            myIPAddr = '192.168.1.10/24'
            bacnet = BAC0.connect(ip = myIPAddr)
            bacnet.read('2:5 analogInput 1 presentValue')

        Requests the controller at (Network 2, address 5) for the presentValue of
        its analog input 1 (AI:1).
        """
        if not self._started:
            raise ApplicationNotStarted(
                'BACnet stack not running - use startApp()')

        args_split = args.split()
        self._log.debug("do_read {!r}".format(args_split))
        vendor_id = vendor_id
        bacoid = bacoid

        try:
            # build ReadProperty request
            iocb = IOCB(self.build_rp_request(
                args_split, arr_index=arr_index, vendor_id=vendor_id, bacoid=bacoid))
            # pass to the BACnet stack
            deferred(self.this_application.request_io, iocb)
            self._log.debug("{:>12} {!r}".format('- iocb', iocb))

        except ReadPropertyException as error:
            # construction error
            self._log.error("exception: {!r}".format(error))

        iocb.wait()             # Wait for BACnet response

        if iocb.ioResponse:     # successful response
            apdu = iocb.ioResponse

            if not isinstance(apdu, ReadPropertyACK):               # expecting an ACK
                self._log.debug("    - not an ack")
                self._log.warning("APDU : {} / {}".format((apdu, type(apdu))))
                return

            # find the datatype
            datatype = get_datatype(
                apdu.objectIdentifier[0], apdu.propertyIdentifier, vendor_id=vendor_id)
            self._log.debug("{:>12} {!r}".format('- datatype', datatype))
            if not datatype:
                raise TypeError("unknown datatype")

            # special case for array parts, others are managed by cast_out
            if issubclass(datatype, Array) and (apdu.propertyArrayIndex is not None):
                if apdu.propertyArrayIndex == 0:
                    value = apdu.propertyValue.cast_out(Unsigned)
                else:
                    value = apdu.propertyValue.cast_out(datatype.subtype)
            else:
                value = apdu.propertyValue.cast_out(datatype)
            self._log.debug("{:>12} {!r}".format('- value', value))

            return value

        if iocb.ioError:        # unsuccessful: error/reject/abort
            apdu = iocb.ioError
            reason = find_reason(apdu)
            if reason == 'segmentationNotSupported':
                self._log.warning(
                    "Segmentation not supported... will read properties one by one...")
                self._log.debug("The Request was : {}".format(args_split))
                value = self._split_the_read_request(args, arr_index)
                return value
            else:
                if reason == 'unknownProperty':
                    self._log.warning('Unknown property {}'.format(args))
                    if 'description' in args:
                        return ''
                    else:
                        raise UnknownPropertyError(
                            'Unknown property {}'.format(args))
                elif reason == 'unknownObject':
                    self._log.warning('Unknown object {}'.format(args))
                    raise UnknownObjectError(
                        'Unknown object {}'.format(args))
                else:
                    # Other error... consider NoResponseFromController (65)
                    # even if the realy reason is another one
                    raise NoResponseFromController(
                        "APDU Abort Reason : {}".format(reason))

    def _split_the_read_request(self, args, arr_index):
        """
        When a device doesn't support segmentation, this function
        will split the request according to the length of the
        predicted result which can be known when readin the array_index
        number 0.

        This can be a very long process as some devices count a large
        number of properties without supporting segmentation
        (FieldServers are a good example)
        """
        objlist = []
        nmbr_obj = self.read(args, arr_index=0)
        for i in range(1, nmbr_obj+1):
            objlist.append(self.read(
                args, arr_index=i))
        return objlist

    def readMultiple(self, args):
        """ Build a ReadPropertyMultiple request, wait for the answer and return the values

        :param args: String with <addr> ( <type> <inst> ( <prop> [ <indx> ] )... )...
        :returns: data read from device (str representing data like 10 or True)

        *Example*::

            import BAC0
            myIPAddr = '192.168.1.10/24'
            bacnet = BAC0.connect(ip = myIPAddr)
            bacnet.readMultiple('2:5 analogInput 1 presentValue units')

        Requests the controller at (Network 2, address 5) for the (presentValue and units) of
        its analog input 1 (AI:1).
        """
        if not self._started:
            raise ApplicationNotStarted(
                'BACnet stack not running - use startApp()')

        args = args.split()
        values = []
        self._log.debug("readMultiple {!r}".format(args))

        try:
            # build an ReadPropertyMultiple request
            iocb = IOCB(self.build_rpm_request(args))
            # pass to the BACnet stack
            deferred(self.this_application.request_io, iocb)

        except ReadPropertyMultipleException as error:
            # construction error
            self._log.error("exception: {!r}".format(error))

        iocb.wait()             # Wait for BACnet response

        if iocb.ioResponse:     # successful response
            apdu = iocb.ioResponse

            if not isinstance(apdu, ReadPropertyMultipleACK):       # expecting an ACK
                self._log.debug("{:>12}".format("- not an ack"))
                self._log.warning("APDU : {} / {}".format((apdu, type(apdu))))
                return

            # loop through the results
            for result in apdu.listOfReadAccessResults:
                # here is the object identifier
                objectIdentifier = result.objectIdentifier
                self._log.debug("{:>12} {!r}".format(
                    '- objectIdentifier', objectIdentifier))
                # now come the property values per object
                for element in result.listOfResults:
                    # get the property and array index
                    propertyIdentifier = element.propertyIdentifier
                    self._log.debug("{:>12} {!r}".format(
                        '- propertyIdentifier', propertyIdentifier))
                    propertyArrayIndex = element.propertyArrayIndex
                    self._log.debug("{:>12} {!r}".format(
                        '- propertyArrayIndex', propertyArrayIndex))

                    readResult = element.readResult

                    if propertyArrayIndex is not None:
                        self._log.debug("[" + str(propertyArrayIndex) + "]")

                    if readResult.propertyAccessError is not None:
                        self._log.debug(
                            " ! " + str(readResult.propertyAccessError))
                        values.append(None)
                    else:
                        # here is the value
                        propertyValue = readResult.propertyValue

                        # find the datatype
                        datatype = get_datatype(
                            objectIdentifier[0], propertyIdentifier)
                        self._log.debug("{:>12} {!r}".format(
                            '- datatype', datatype))
                        if not datatype:
                            raise TypeError("unknown datatype")

                        # special case for array parts, others are managed by cast_out
                        if issubclass(datatype, Array) and (propertyArrayIndex is not None):
                            if propertyArrayIndex == 0:
                                value = propertyValue.cast_out(Unsigned)
                            else:
                                value = propertyValue.cast_out(
                                    datatype.subtype)
                        else:
                            value = propertyValue.cast_out(datatype)
                            self._log.debug("{:>12} {!r}".format(
                                '- value', value))
                        values.append(value)

            return values

        if iocb.ioError:        # unsuccessful: error/reject/abort
            apdu = iocb.ioError
            reason = find_reason(apdu)
            self._log.warning("APDU Abort Reject Reason : {}".format(reason))
            self._log.debug("The Request was : {}".format(args))
            if reason == 'unrecognizedService':
                raise UnrecognizedService()
            elif reason == 'segmentationNotSupported':
                raise SegmentationNotSupported()
            elif reason == 'unknownObject':
                self._log.warning('Unknown object {}'.format(args))
                raise UnknownObjectError('Unknown object {}'.format(args))
            elif reason == 'unknownProperty':
                self._log.warning('Unknown property {}'.format(args))
                values.append("")
                return values
            else:
                self._log.warning("No response from controller")
                values.append("")
                return values

    def build_rp_request(self, args, arr_index=None, vendor_id=0, bacoid=None):
        addr, obj_type, obj_inst, prop_id = args[:4]
        vendor_id = vendor_id
        bacoid = bacoid

        if obj_type.isdigit():
            obj_type = int(obj_type)
        elif not get_object_class(obj_type):
            raise ValueError("unknown object type")

        obj_inst = int(obj_inst)

        if prop_id.isdigit():
            prop_id = int(prop_id)
        datatype = get_datatype(obj_type, prop_id, vendor_id=vendor_id)
        if not datatype:
            raise ValueError("invalid property for object type")

        # build a request
        request = ReadPropertyRequest(
            objectIdentifier=(obj_type, obj_inst),
            propertyIdentifier=prop_id,
            propertyArrayIndex=arr_index,
        )
        request.pduDestination = Address(addr)

        if len(args) == 5:
            request.propertyArrayIndex = int(args[4])
        self._log.debug("{:>12} {!r}".format(
            '- request', request))
        return request

    def build_rpm_request(self, args):
        """
        Build request from args
        """
        i = 0
        addr = args[i]
        i += 1

        read_access_spec_list = []
        while i < len(args):
            obj_type = args[i]
            i += 1

            if obj_type.isdigit():
                obj_type = int(obj_type)
            elif not get_object_class(obj_type):
                raise ValueError("unknown object type")

            obj_inst = int(args[i])
            i += 1

            prop_reference_list = []
            while i < len(args):
                prop_id = args[i]
                if prop_id not in PropertyIdentifier.enumerations:
                    break

                i += 1
                if prop_id in ('all', 'required', 'optional'):
                    pass
                else:
                    datatype = get_datatype(obj_type, prop_id)
                    if not datatype:
                        raise ValueError(
                            "invalid property for object type : {} | {}".format(
                                (obj_type, prop_id)))

                # build a property reference
                prop_reference = PropertyReference(propertyIdentifier=prop_id)

                # check for an array index
                if (i < len(args)) and args[i].isdigit():
                    prop_reference.propertyArrayIndex = int(args[i])
                    i += 1

                prop_reference_list.append(prop_reference)

            if not prop_reference_list:
                raise ValueError("provide at least one property")

            # build a read access specification
            read_access_spec = ReadAccessSpecification(
                objectIdentifier=(obj_type, obj_inst),
                listOfPropertyReferences=prop_reference_list)

            read_access_spec_list.append(read_access_spec)

        if not read_access_spec_list:
            raise RuntimeError(
                "at least one read access specification required")

        # build the request
        request = ReadPropertyMultipleRequest(
            listOfReadAccessSpecs=read_access_spec_list)
        request.pduDestination = Address(addr)
        self._log.debug("{:>12} {!r}".format(
            '- request', request))
        return request


def find_reason(apdu):
    if apdu.pduType == RejectPDU.pduType:
        reasons = RejectReason.enumerations
    elif apdu.pduType == AbortPDU.pduType:
        reasons = AbortReason.enumerations
    else:
        if apdu.errorCode and apdu.errorClass:
            return '{}'.format(apdu.errorCode)
        else:
            self._log.warning('Cannot identify error : {}'.format(
                              apdu.__dict__))
            return 'UnKnown Error...'
    code = apdu.apduAbortRejectReason
    try:
        return [k for k, v in reasons.items() if v == code][0]
    except IndexError:
        return code
