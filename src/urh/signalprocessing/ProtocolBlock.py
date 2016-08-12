import copy
import math
import xml.etree.ElementTree as ET
import sys

import numpy as np
from urh.cythonext.signalFunctions import Symbol

from urh import constants
from urh.signalprocessing.ProtocoLabel import ProtocolLabel

from urh.signalprocessing.LabelSet import LabelSet
from urh.signalprocessing.encoding import encoding
from urh.util.Formatter import Formatter
from urh.util.Logger import logger


class ProtocolBlock(object):
    """
    A protocol block is a single line of a protocol.
    """

    __slots__ = ["__plain_bits", "pause", "modulator_indx", "rssi", "participant", "labelset",
                 "absolute_time", "relative_time", "__decoder", "align_labels",
                 "fuzz_created", "__decoded_bits", "__encoded_bits", "decoding_errors", "bit_len", "bit_sample_pos"]

    def __init__(self, plain_bits, pause: int, labelset: LabelSet, rssi=0, modulator_indx=0, decoder=None,
                 fuzz_created=False, bit_sample_pos=None, bit_len=100, participant=None):
        """

        :param pause: Pause NACH dem Block in Samples
        :type plain_bits: list[bool|Symbol]
        :type decoder: encoding
        :type bit_alignment_positions: list of int
        :param bit_alignment_positions: Für Ausrichtung der Hex Darstellung (Leere Liste für Standardverhalten)
        :param bit_len: Für Übernahme der Bitlänge in Modulator Dialog
        :param fuzz_created: Block was created thrugh fuzzing
        :return:
        """
        self.__plain_bits = plain_bits
        self.pause = pause
        self.modulator_indx = modulator_indx
        self.rssi = rssi
        self.participant = participant
        """:type: Participant """

        self.labelset = labelset
        """:type: LabelSet """

        self.absolute_time = 0  # set in Compare Frame
        self.relative_time = 0  # set in Compare Frame

        self.__decoder = decoder if decoder else encoding(["Non Return To Zero (NRZ)"])
        """:type: encoding """

        self.align_labels = constants.SETTINGS.value("align_labels", True, bool)
        self.fuzz_created = fuzz_created

        self.__decoded_bits = None
        self.__encoded_bits = None
        self.decoding_errors = 0

        self.bit_len = bit_len  # Für Übernahme in Modulator

        if bit_sample_pos is None:
            self.bit_sample_pos = []
        else:
            self.bit_sample_pos = bit_sample_pos
            """
            :param bit_sample_pos: Position of samples for each bit. Last position is pause so last bit is on pos -2.
            :type  bit_sample_pos: list of int
            """

    @property
    def plain_bits(self):
        """

        :rtype: list[bool|Symbol]
        """
        return self.__plain_bits

    @plain_bits.setter
    def plain_bits(self, value):
        self.__plain_bits = value
        self.clear_decoded_bits()
        self.clear_encoded_bits()


    @property
    def active_fuzzing_labels(self):
        return [lbl for lbl in self.labelset if lbl.active_fuzzing]

    @property
    def exclude_from_decoding_labels(self):
        return [lbl for lbl in self.labelset if not lbl.apply_decoding]

    def __getitem__(self, index: int):
        return self.plain_bits[index]

    def __setitem__(self, index: int, value):
        """

        :type value: bool or Symbol
        """
        self.plain_bits[index] = value
        self.clear_decoded_bits()
        self.clear_encoded_bits()

    def __add__(self, other):
        return self.__plain_bits + other.__plain_bits

    def __delitem__(self, index):
        if isinstance(index, slice):
            step = index.step
            if step is None:
                step = 1
            number_elements = len(range(index.start, index.stop, step))

            for l in self.labelset[:]:
                if index.start <= l.start and index.stop >= l.end:
                    self.labelset.remove(l)

                elif index.stop - 1 < l.start:
                    l_cpy = copy.deepcopy(l)
                    l_cpy.start -= number_elements
                    l_cpy.end -= number_elements
                    self.labelset.remove(l)
                    self.labelset.append(l_cpy)

                elif index.start <= l.start and index.stop >= l.start:
                    self.labelset.remove(l)

                elif index.start >= l.start and index.stop <= l.end:
                    self.labelset.remove(l)

                elif index.start >= l.start and index.start < l.end:
                    self.labelset.remove(l)
        else:
            for l in self.labelset:
                if index < l.start:
                    l_cpy = copy.deepcopy(l)
                    l_cpy.start -= 1
                    l_cpy.end -= 1
                    self.labelset.remove(l)
                    self.labelset.append(l_cpy)
                elif l.start < index < l.end:
                    l_cpy = copy.deepcopy(l)
                    l_cpy.start = index - 1
                    self.labelset.remove(l)
                    if l_cpy.end - l_cpy.start > 0:
                        self.labelset.append(l_cpy)

        del self.plain_bits[index]

    def __str__(self):
        return self.bits2string(self.plain_bits)

    def get_byte_length(self, decoded=True) -> int:
        """
        Return the length of this block in byte.

        """
        end = len(self.decoded_bits) if decoded else len(self.__plain_bits)
        end = self.convert_index(end, 0, 2, decoded=decoded)[0]
        return int(end)

    def get_bytes(self, start=0, decoded=True) -> list:
        data = self.decoded_ascii_str[start:] if decoded else self.plain_ascii_str[start:]
        return list(map(ord, data))

    def bits2string(self, bits) -> str:
        """

        :type bits: list[bool|Symbol]
        """
        return "".join(bit.name if type(bit) == Symbol else "1" if bit else "0" for bit in bits)

    def string2bits(self, string: str):
        """
        Does not Accept Symbols!

        :param string:
        :rtype: list[bool]
        """
        if any(c not in ("0", "1") for c in string):
            raise ValueError("String2Bits: Only Bits accepted")

        return [True if c == "1" else "0" for c in string]

    def __len__(self):
        return len(self.plain_bits)

    def insert(self, index: int, item: bool):
        self.plain_bits.insert(index, item)
        self.__decoded_bits = None

    @property
    def decoder(self) -> encoding:
        return self.__decoder


    @decoder.setter
    def decoder(self, val: encoding):
        self.__decoder = val
        self.clear_decoded_bits()
        self.clear_encoded_bits()
        self.decoding_errors = self.decoder.analyze(self.plain_bits)


    @property
    def encoded_bits(self):
        """

        :rtype: list[bool|Symbol]
        """
        if self.__encoded_bits is None:
            self.__encoded_bits = []
            start = 0
            encode = self.decoder.encode
            bits = self.plain_bits
            symbol_indexes = [i for i, b in enumerate(self.plain_bits) if type(b) == Symbol]
            for plabel in self.exclude_from_decoding_labels:
                symindxs = [i for i in symbol_indexes if i in range(start, plabel.start)]
                tmp = start
                for si in symindxs:
                    self.__encoded_bits.extend(encode(bits[tmp:si]) + [bits[si]])
                    tmp = si + 1

                self.__encoded_bits.extend(encode(bits[tmp:plabel.start]))
                start = plabel.start if plabel.start > start else start  # Overlapping
                self.__encoded_bits.extend(bits[start:plabel.end])
                start = plabel.end if plabel.end > start else start  # Overlapping

            symindxs = [i for i in symbol_indexes if i >= start]
            tmp = start
            for si in symindxs:
                self.__encoded_bits.extend(encode(bits[tmp:si]) + [bits[si]])
                tmp = si + 1
            self.__encoded_bits.extend(encode(bits[tmp:]))
        return self.__encoded_bits

    @property
    def encoded_bits_str(self) -> str:
        return self.bits2string(self.encoded_bits)

    @property
    def decoded_bits(self):
        """

        :rtype: list[bool|Symbol]
        """
        if self.__decoded_bits is None:
            self.__decoded_bits = []
            start = 0
            code = self.decoder.code  # 0 = decoded, 1 = analyzed
            # decode = self.decoder.decode
            # analyze = self.decoder.analyze
            bits = self.plain_bits
            self.decoding_errors = 0
            symbol_indexes = [i for i, b in enumerate(self.plain_bits) if type(b) == Symbol]
            for plabel in self.exclude_from_decoding_labels:
                symindxs = [i for i in symbol_indexes if i in range(start, plabel.start)]
                tmp = start
                for si in symindxs:
                    decoded, errors = code(True, bits[tmp:si])
                    self.__decoded_bits.extend(decoded + [bits[si]])
                    self.decoding_errors += errors
                    # self.__decoded_bits.extend(decode(bits[tmp:si]) + [bits[si]])
                    #self.decoding_errors += analyze(bits[tmp:si])
                    tmp = si + 1


                # self.__decoded_bits.extend(decode(bits[tmp:plabel.start]))
                decoded, errors = code(True, bits[tmp:plabel.start])
                self.__decoded_bits.extend(decoded)
                self.decoding_errors += errors

                if plabel.start == -1 or plabel.end == -1:
                    plabel.start = len(self.__decoded_bits)
                    plabel.end = plabel.start + (plabel.end - plabel.start)

                    #self.decoding_errors += analyze(bits[tmp:plabel.start])

                start = plabel.start if plabel.start > start else start  # Überlappende Labels -.-
                self.__decoded_bits.extend(bits[start:plabel.end])
                start = plabel.end if plabel.end > start else start  # Überlappende Labels FFS >.<

            symindxs = [i for i in symbol_indexes if i >= start]
            tmp = start
            for si in symindxs:
                decoded, errors = code(True, bits[tmp:si])
                self.__decoded_bits.extend(decoded + [bits[si]])
                self.decoding_errors += errors

                # self.__decoded_bits.extend(decode(bits[tmp:si]) + [bits[si]])
                #self.decoding_errors += analyze(bits[tmp:si])
                tmp = si + 1

            decoded, errors = code(True, bits[tmp:])
            self.__decoded_bits.extend(decoded)
            self.decoding_errors += errors
            # self.__decoded_bits.extend(decode(bits[tmp:]))
            # self.decoding_errors += analyze(bits[tmp:])

        return self.__decoded_bits

    @decoded_bits.setter
    def decoded_bits(self, val):
        """
        :type val: list[bool|Symbol]
        """
        self.__decoded_bits = val

    @property
    def decoded_bits_str(self) -> str:
        return self.bits2string(self.decoded_bits)

    @property
    def plain_bits_str(self) -> str:
        return str(self)

    @property
    def decoded_bits_buffer(self) -> bytes:
        bits = [b if isinstance(b, bool) else True if b.pulsetype == 1 else False for b in self.decoded_bits]
        return np.packbits(bits).tobytes()

    @property
    def plain_hex_str(self) -> str:
        padded_bitchains = self.split(decode=False)
        return self.__bitchains_to_hex(padded_bitchains)


    @property
    def plain_ascii_str(self) -> str:
        padded_bitchains = self.split(decode=False)
        return self.__bitchains_to_ascii(padded_bitchains)

    @property
    def decoded_hex_str(self) -> str:
        padded_bitchains = self.split()
        return self.__bitchains_to_hex(padded_bitchains)


    @property
    def decoded_ascii_str(self) -> str:
        padded_bitchains = self.split()
        return self.__bitchains_to_ascii(padded_bitchains)

    def __get_bit_range_from_hex_or_ascii_index(self, from_index: int, decoded: bool, is_hex: bool) -> tuple:
        bits = self.decoded_bits if decoded else self.plain_bits
        factor = 4 if is_hex else 8
        pos = 0
        cur_index = 0
        result = 0
        # TODO Consider Bit alignment for labels (if label align is enabled)

        for si in (i for i, b in enumerate(bits) if type(b) == Symbol):
            if from_index > cur_index + math.ceil((si - pos) / factor):
                result += (si - pos) + 1
                cur_index += math.ceil((si - pos) / factor) + 1
                pos = si + 1
            elif from_index == cur_index + math.ceil((si - pos) / factor):
                result += (si - pos)
                return result, result
            else:
                break

        if from_index > cur_index:
            result += factor * (from_index - cur_index)

        end = result + factor - 1
        #end = end if end < len(bits) else len(bits) - 1

        return result, end

    def __get_hex_ascii_index_from_bit_index(self, bit_index: int, decoded: bool, to_hex: bool) -> tuple:
        bits = self.decoded_bits if decoded else self.plain_bits
        factor = 4 if to_hex else 8
        pos = 0
        result = 0

        # TODO Consider Bit alignment for labels (if label align is enabled)

        for si in (i for i, b in enumerate(bits) if type(b) == Symbol):
            if bit_index > si:
                result += math.ceil((si - pos) / factor) + 1
                pos = si + 1
            elif bit_index == si:
                result += math.ceil((si - pos) / factor)
                return result, result
            else:
                break

        if pos < bit_index:
            result += math.ceil((bit_index - pos) / factor)

        return result, result

    def convert_index(self, index: int, from_view: int, to_view: int, decoded: bool):
        if to_view == from_view:
            return index, index

        if to_view == 0:
            return self.__get_bit_range_from_hex_or_ascii_index(index, decoded, is_hex=from_view == 1)
        if to_view == 1:
            if from_view == 0:
                return self.__get_hex_ascii_index_from_bit_index(index, decoded, to_hex=True)
            elif from_view == 2:
                bi = self.__get_bit_range_from_hex_or_ascii_index(index, decoded, is_hex=True)[0]
                return self.__get_hex_ascii_index_from_bit_index(bi, decoded, to_hex=False)
        elif to_view == 2:
            if from_view == 0:
                return self.__get_hex_ascii_index_from_bit_index(index, decoded, to_hex=False)
            elif from_view == 1:
                bi = self.__get_bit_range_from_hex_or_ascii_index(index, decoded, is_hex=False)[0]
                return self.__get_hex_ascii_index_from_bit_index(bi, decoded, to_hex=True)
        else:
            raise NotImplementedError("Only Three View Types (Bit/Hex/ASCII)")

    def convert_range(self, index1: int, index2: int, from_view: int, to_view: int, decoded: bool):
        start = self.convert_index(index1, from_view, to_view, decoded)[0]
        end = self.convert_index(index2, from_view, to_view, decoded)[1]

        return int(start), int(math.ceil(end))

    def get_duration(self, sample_rate: int) -> float:
        if len(self.bit_sample_pos) < 2:
            raise ValueError("Not enough bit samples for calculating duration")

        return (self.bit_sample_pos[-1] - self.bit_sample_pos[0]) / sample_rate

    @staticmethod
    def __bitchains_to_hex(bitchains) -> str:
        """

        :type bitchains: list of str
        :return:
        """
        result = []
        for bitchain in bitchains:
            if len(bitchain) == 1 and bitchain not in ("0", "1"):
                # Symbol
                result.append(bitchain)
            else:
                result.append("".join("{0:x}".format(int(bitchain[i:i + 4], 2)) for i in range(0, len(bitchain), 4)))
        return "".join(result)

    @staticmethod
    def __bitchains_to_ascii(bitchains) -> str:
        """

        :type bitchains: list of str
        :return:
        """
        result = []
        for bitchain in bitchains:
            if len(bitchain) == 1 and bitchain not in ("0", "1"):
                # Symbol
                result.append(bitchain)
            else:
                byte_proto = "".join("{0:x}".format(int(bitchain[i:i + 8], 2)) for i in range(0, len(bitchain), 8))
                result.append("".join(chr(int(byte_proto[i:i + 2], 16)) for i in range(0, len(byte_proto) - 1, 2)))

        return "".join(result)


    def split(self, decode=True):
        """
        Für das Bit-Alignment (neu Ausrichten von Hex, ASCII-View)

        :rtype: list of str
        """
        start = 0
        result = []
        block = self.decoded_bits_str if decode else str(self)
        symbol_indexes = [i for i, b in enumerate(block) if b not in ("0", "1")]
        bit_alignment_positions = []
        if self.align_labels:
            for l in self.labelset:
                bit_alignment_positions.append(l.start)
                bit_alignment_positions.append(l.end)

        for pos in bit_alignment_positions:
            sym_indx = [i for i in symbol_indexes if i < pos]
            for si in sym_indx:
                result.append(block[start:si])
                result.append(block[si])
                start = si +1
            result.append(block[start:pos])
            start = pos

        sym_indx = [i for i in symbol_indexes if i >= start]
        for si in sym_indx:
            result.append(block[start:si])
            result.append(block[si])
            start = si+1

        result.append(block[start:])
        return result

    def view_to_string(self, view: int, decoded: bool, show_pauses=True,
                       sample_rate: float = None) -> str:
        """

        :param view: 0 - Bits ## 1 - Hex ## 2 - ASCII
        """
        if view == 0:
            proto = self.decoded_bits_str if decoded else self.plain_bits_str
        elif view == 1:
            proto = self.decoded_hex_str if decoded else self.plain_hex_str
        elif view == 2:
            proto = self.decoded_ascii_str if decoded else self.plain_ascii_str
        else:
            return None

        if show_pauses:
            return '%s %s' % (proto, self.get_pause_str(sample_rate))
        else:
            return proto


    def get_pause_str(self, sample_rate):
        if sample_rate:
            return ' [<b>Pause:</b> %s]' % (Formatter.science_time(self.pause / sample_rate))
        else:
            return ' [<b>Pause:</b> %d samples]' % (self.pause)


    def clear_decoded_bits(self):
        self.__decoded_bits = None

    def clear_encoded_bits(self):
        self.__encoded_bits = None

    @staticmethod
    def from_plain_bits_str(bits, symbols: dict):
        plain_bits = []
        for b in bits:
            if b == "0":
                plain_bits.append(False)
            elif b == "1":
                plain_bits.append(True)
            else:
                try:
                    plain_bits.append(symbols[b])
                except KeyError:
                    print("[Warning] Did not find symbol name", file=sys.stderr)
                    plain_bits.append(Symbol(b, 0, 0, 1))

        return ProtocolBlock(plain_bits=plain_bits, pause=0, labelset=LabelSet("none"))

    def to_xml(self, decoders=None, include_labelset=False) -> ET.Element:
        root = ET.Element("block")
        root.set("labelset_id", self.labelset.id)
        root.set("modulator_index", str(self.modulator_indx))
        root.set("pause", str(self.pause))
        if decoders:
            root.set("decoding_index", str(decoders.index(self.decoder)))
        if self.participant is not None:
            root.set("participant_id",  self.participant.id)
        if include_labelset:
            root.append(self.labelset.to_xml())
        return root

    def from_xml(self, tag: ET.Element, participants, decoders=None, labelsets=None):
        part_id = tag.get("participant_id", None)
        labelset_id = tag.get("labelset_id", None)
        self.modulator_indx = int(tag.get("modulator_index", self.modulator_indx))
        self.pause = int(tag.get("pause", self.pause))
        decoding_index = tag.get("decoding_index", None)
        if decoding_index:
            try:
                self.decoder = decoders[int(decoding_index)]
            except IndexError:
                pass

        if part_id:
            for participant in participants:
                if participant.id_match(part_id):
                    self.participant = participant
                    break
            if self.participant is None:
                logger.warning("No participant matched the id {0} from xml".format(part_id))

        if labelset_id and labelsets:
            for labelset in labelsets:
                if labelset.id == labelset_id:
                    self.labelset = labelset
                    break

        labelset_tag = tag.find("labelset")
        if labelset_tag:
            self.labelset = LabelSet.from_xml(labelset_tag)


    def get_label_range(self, lbl: ProtocolLabel, view: int, decode: bool):
        start = self.convert_index(index=lbl.start, from_view=0, to_view=view, decoded=decode)[0]
        end = self.convert_index(index=lbl.end, from_view=0, to_view=view, decoded=decode)[1]
        return int(start), int(end)

    def find_preamble_end(self):
        try:
            start = 0 if self.decoded_bits[0] else 1
            for i in range(start, len(self.decoded_bits), constants.SHORTEST_PREAMBLE_IN_BITS):
                for j in range(0, constants.SHORTEST_PREAMBLE_IN_BITS, 2):
                    if not(self.decoded_bits[i+j] and not self.decoded_bits[i+j+1]):
                        return i - 1 if i - 1 >= 0 else None

            return len(self.decoded_bits) - 1 if len(self.decoded_bits) - 1 >= 0 else None

        except IndexError:
            return None