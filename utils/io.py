# TODO: !!! Switch to using Kaolin version

# Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES.
# All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import math
import collections
import json
import logging
import numpy as np
import torch
from enum import IntEnum


logger = logging.getLogger(__name__)

class BinaryIoDataType(IntEnum):
    INT8 = 0
    UINT8 = 1
    INT16 = 2
    INT32 = 3
    UINT32 = 4
    INT64 = 5
    FLOAT16 = 6
    FLOAT32 = 7
    FLOAT64 = 8
    STRING = 9
    DICT = 10
    LIST = 11
    UNSUPPORTED = 100


__np_type_mappings =  [(BinaryIoDataType.INT8, np.dtype(np.int8)),
                       (BinaryIoDataType.UINT8, np.dtype(np.uint8)),
                       (BinaryIoDataType.INT16, np.dtype(np.int16)),
                       (BinaryIoDataType.INT32, np.dtype(np.int32)),
                       (BinaryIoDataType.UINT32, np.dtype(np.uint32)),
                       (BinaryIoDataType.INT64, np.dtype(np.int64)),
                       (BinaryIoDataType.FLOAT16, np.dtype(np.float16)),
                       (BinaryIoDataType.FLOAT32, np.dtype(np.float32)),
                       (BinaryIoDataType.FLOAT64, np.dtype(np.float64))]
__np_type_to_bytes = {np.dtype(np.int8): 1,
                      np.dtype(np.uint8): 1,
                      np.dtype(np.int16): 2,
                      np.dtype(np.int32): 4,
                      np.dtype(np.uint32): 4,
                      np.dtype(np.int64): 8,
                      np.dtype(np.float16): 2,
                      np.dtype(np.float32): 4,
                      np.dtype(np.float64): 8}
__io_data_type_to_np = dict(__np_type_mappings)
__np_to_io_data_type = dict([(x[1], x[0]) for x in __np_type_mappings])

MESSAGE_TAG_KEY = 'tag'
MESSAGE_CONTENT_KEY = 'msg'


def encode_message(tag, content, binary=True):
    """Encode a tagged message as binary bytes or JSON text.

    Example:
        >>> payload = encode_message("ping", {"ok": 1}, binary=False)
        >>> payload
        '{"tag": "ping", "msg": {"ok": 1}}'
    """
    msg = {MESSAGE_TAG_KEY: tag, MESSAGE_CONTENT_KEY: content}
    if binary:
        return to_binary(msg)
    else:
        return json.dumps(msg)


def to_binary(value):
    """Serialize a supported value into the project's aligned binary format."""
    return value_to_binary(value, 0)


def from_binary(bytes_msg: bytes):
    """Deserialize a value encoded with :func:`to_binary`.

    Example:
        >>> from_binary(to_binary({"value": 3}))["value"]
        3
    """
    res, read_bytes = value_from_binary(bytes_msg, 0)
    if read_bytes != len(bytes_msg):
        logger.warning(f'Read {read_bytes}, not full message length {len(bytes_msg)}')
    return res


def np_type_from_type_id(type_id):
    """
    Returns NumPy dtype and bytes per element for given type ID.

    Args:
        type_id: Binary I/O data type ID

    Returns:
        tuple: (numpy_dtype, bytes_per_element) or (None, 0) if unknown
    """
    np_type = __io_data_type_to_np.get(type_id, None)
    if np_type is not None:
        num_bytes = __np_type_to_bytes.get(np_type, 0)
    else:
        num_bytes = 0

    return np_type, num_bytes


def value_to_type(converted_value):
    """Return the binary type enum used to encode ``converted_value``."""
    if isinstance(converted_value, str):
        return BinaryIoDataType.STRING
    elif isinstance(converted_value, collections.abc.Mapping):
        return BinaryIoDataType.DICT
    elif isinstance(converted_value, list):
        return BinaryIoDataType.LIST
    elif isinstance(converted_value, np.ndarray):
        return __np_to_io_data_type.get(converted_value.dtype, BinaryIoDataType.UNSUPPORTED)
    else:
        return BinaryIoDataType.UNSUPPORTED


def convert_value_to_supported_format(value):
    """Normalize Python values into encodable strings, mappings, lists, or arrays.

    Torch tensors are moved to CPU NumPy arrays. Numeric lists are packed into
    NumPy arrays, while mixed-type lists stay as Python lists.
    """
    if isinstance(value, str):
        return value
    elif torch.is_tensor(value):
        return value.detach().cpu().numpy()
    elif isinstance(value, collections.abc.Mapping):
        return value
    elif isinstance(value, np.ndarray):
        # TODO: possibly convert type
        return value
    elif isinstance(value, list):
        if len(value) == 0:
            return value  # Keep empty lists as lists
        
        # Check if all elements are numbers (int or float)
        all_numbers = all(isinstance(item, (int, float, bool)) for item in value)
        
        if all_numbers:
            # Check if all are integers
            all_integers = all(isinstance(item, int) or isinstance(item, bool) for item in value)
            if all_integers:
                return np.array(value, dtype=np.int32)
            else:
                return np.array(value, dtype=np.float32)
        else:
            # Mixed types - keep as list for LIST support
            return value
    elif isinstance(value, int):
        return np.array([value], dtype=np.int32)
    elif isinstance(value, float):
        return np.array([value], dtype=np.float32)
    else:
        raise ValueError(f'Cannot encode value of type {type(value)} to binary')


def gap_until_offset_n(current_offset: int, n: int) -> int:
    """
    Calculate the gap needed to align current_offset to a multiple of n.
    Arrays such as Int32Array cannot start at offsets that are not
    a multiple of 4. This helps us find the right offset.

    Args:
        current_offset: Current byte offset
        n: Alignment requirement (e.g., 4 for 4-byte alignment)

    Returns:
        Number of bytes to add to reach proper alignment
    """
    return (n - (current_offset % n)) % n


def gap_until_offset_4(current_offset: int) -> int:
    """Return padding bytes needed to align ``current_offset`` to 4 bytes."""
    return gap_until_offset_n(current_offset, 4)


def string_from_binary(bytes_msg, offset, byte_length):
    """
    Decodes UTF-8 string of specified length from binary data.

    Args:
        bytes_msg: Input binary data (bytes)
        offset: Byte offset in data
        byte_length: String length in bytes

    Returns:
        Decoded string
    """
    if byte_length == 0:
        return ''
    string_bytes = bytes_msg[offset:offset + byte_length]
    return string_bytes.decode('utf-8')


def string_to_binary(string):
    """
    Encodes string to binary data using UTF-8 encoding.

    Args:
        string: String to encode

    Returns:
        bytes containing UTF-8 encoded bytes
    """
    return string.encode('utf-8')


def typed_value_from_binary(bytes_msg, offset, length, type_code):
    """Decode a value of a known binary type from ``bytes_msg``.

    Returns:
        tuple[Any, int]: The decoded value and the number of bytes consumed from
        ``offset`` onward.
    """
    # Length is byte length for strings, but num elements for other types
    if type_code == BinaryIoDataType.STRING:
        return string_from_binary(bytes_msg, offset, length), length
    elif type_code == BinaryIoDataType.DICT:
        value, read_bytes = _dict_from_binary(bytes_msg, length=length, offset=offset)
        return value, read_bytes
    elif type_code == BinaryIoDataType.LIST:
        value, read_bytes = _list_from_binary(bytes_msg, length=length, offset=offset)
        return value, read_bytes
    else:
        np_type, bytes_per_element = np_type_from_type_id(type_code)
        if np_type is None:
            return None, 0
        read_bytes = gap_until_offset_n(offset, bytes_per_element)
        value = np.frombuffer(bytes_msg, dtype=np_type, count=length, offset=offset + read_bytes)
        read_bytes += length * bytes_per_element
        return value, read_bytes


def value_from_binary(bytes_msg, offset):
    """Decode one aligned value starting at ``offset``.

    Returns:
        tuple[Any, int]: The decoded value and total bytes consumed, including
        alignment padding and metadata.
    """
    read_bytes = gap_until_offset_4(offset)

    metadata_length = 2
    metadata = np.frombuffer(bytes_msg, dtype=np.int32, count=metadata_length, offset=offset + read_bytes)
    read_bytes += metadata_length * 4
    shape_length = metadata[0]
    type_code = metadata[1]

    is_primitive = False
    if shape_length > 0:
        shape = np.frombuffer(bytes_msg, dtype=np.int32, count=shape_length, offset=offset + read_bytes)
        read_bytes += shape_length * 4
        length = np.prod(shape)
    else:
        length = 1
        is_primitive = True
    value, value_read_bytes = typed_value_from_binary(bytes_msg, offset + read_bytes, length, type_code)
    read_bytes += value_read_bytes
    if isinstance(value, np.ndarray):
        if is_primitive:
            value = value[0].item()
        else:
            try:
                # TODO: this array is not writable; figure out what the behavior should be
                value = torch.from_numpy(value).reshape([x for x in shape])  # return in torch, as that is Kaolin i/o convention
            except ValueError as e:
                logger.error(f'Decoded shape does not match value size {e}')
    return value, read_bytes


def value_to_binary(in_value, initial_offset=0):
    """Encode a supported value starting at ``initial_offset``.

    Args:
        in_value: Value to encode.
        initial_offset: Existing byte offset used to compute alignment padding.

    Returns:
        bytes: Encoded representation of ``in_value``.

    Raises:
        ValueError: If the value cannot be represented by the binary protocol.
    """
    is_primitive_number = isinstance(in_value, int) or isinstance(in_value, float)
    value = convert_value_to_supported_format(in_value)

    type_code = value_to_type(value)
    if type_code == BinaryIoDataType.UNSUPPORTED:
        raise ValueError(f'Cannot encode value of type {type(value)}')

    # Insert alignment
    result = bytes(gap_until_offset_4(initial_offset))

    if type_code == BinaryIoDataType.STRING:
        encoded_value = string_to_binary(value)
        shape = np.array([len(encoded_value)], dtype=np.int32)
        bytes_per_elem = 1
    elif type_code == BinaryIoDataType.DICT:
        shape = np.array([len(value)], dtype=np.int32)
        encoded_value = _dict_to_binary(value, initial_offset=initial_offset + len(result) + 3 * 4)
        bytes_per_elem = 1  # alignment is already accounted for
    elif type_code == BinaryIoDataType.LIST:
        shape = np.array([len(value)], dtype=np.int32)
        encoded_value = _list_to_binary(value, initial_offset=initial_offset + len(result) + 3 * 4)
        bytes_per_elem = 1  # alignment is already accounted for
    else:
        encoded_value = value.tobytes()
        # set shape len to 0 for primitives
        shape = np.array([] if is_primitive_number else value.shape, dtype=np.int32)
        bytes_per_elem = __np_type_to_bytes.get(value.dtype, 1)

    # Encode metadata and shape
    result += np.array([len(shape), type_code], dtype=np.int32).tobytes() + shape.tobytes()

    # Insert alignment
    extra_bytes = gap_until_offset_n(len(result) + initial_offset, bytes_per_elem)
    result += bytes(extra_bytes)
    result += encoded_value
    return result


def named_value_from_binary(bytes_msg, offset):
    """Decode a length-prefixed key followed by a binary-encoded value."""
    read_bytes = gap_until_offset_4(offset)
    name_length = np.frombuffer(bytes_msg, dtype=np.int32, count=1, offset=offset + read_bytes)[0]  # in bytes
    read_bytes += 4
    name = string_from_binary(bytes_msg, offset + read_bytes, name_length)
    read_bytes += name_length
    value, value_read_bytes = value_from_binary(bytes_msg, offset + read_bytes)
    read_bytes += value_read_bytes
    return name, value, read_bytes


def named_value_to_binary(name, value, initial_offset=0):
    """Encode a string key followed by a binary-encoded value."""
    # We assume offset is appropriate for int32
    bin_str = string_to_binary(name)
    result = bytes(gap_until_offset_4(initial_offset))
    result += int32_to_binary(len(bin_str))
    result += bin_str
    result += value_to_binary(value, initial_offset=initial_offset + len(result))
    return result


def _dict_from_binary(bytes_msg, length, offset=0):
    """Decode ``length`` named values into a dictionary.

    Args:
        bytes_msg: Raw bytes to decode.
        length: Number of key-value pairs to read.
        offset: Start read offset in bytes.

    Returns:
        tuple[dict, int]: Decoded mapping and total bytes consumed.
    """
    total_read_bytes = 0
    res = {}
    for i in range(length):
        name, value, read_bytes = named_value_from_binary(bytes_msg, offset + total_read_bytes)
        res[name] = value
        total_read_bytes += read_bytes

    return res, total_read_bytes


def _list_from_binary(bytes_msg, length, offset=0):
    """Decode ``length`` sequential values into a list.

    Args:
        bytes_msg: Raw bytes to decode.
        length: Number of elements to read.
        offset: Start read offset in bytes.

    Returns:
        tuple[list, int]: Decoded list and total bytes consumed.
    """
    total_read_bytes = 0
    res = []
    for i in range(length):
        value, read_bytes = value_from_binary(bytes_msg, offset + total_read_bytes)
        res.append(value)
        total_read_bytes += read_bytes

    return res, total_read_bytes



def int32_to_binary(single_int):
    """Encode one integer as a 4-byte little-endian NumPy int32 buffer."""
    return np.array([single_int], dtype=np.int32).tobytes()


def _dict_to_binary(in_dict, initial_offset=0):
    """Encode a dictionary as sequential named values."""
    result = bytes()

    for name, value in in_dict.items():
        result += named_value_to_binary(name, value, initial_offset=initial_offset + len(result))
    return result


def _list_to_binary(in_list, initial_offset=0):
    """Encode a list as sequential binary values."""
    result = bytes()

    for value in in_list:
        result += value_to_binary(value, initial_offset=initial_offset + len(result))
    return result

def split_tensor(tensor, max_chunk_bytes=1_000_000):
    """Split a tensor into contiguous chunks no larger than ``max_chunk_bytes``."""
    bytes_per_elem = tensor.element_size()        # e.g., float32 = 4 bytes
    total_elems = tensor.numel()
    elems_per_chunk = max_chunk_bytes // bytes_per_elem
    elems_per_chunk = max(1, elems_per_chunk)

    flat = tensor.contiguous().view(-1)

    chunks = [
        flat[i:i+elems_per_chunk].clone()
        for i in range(0, total_elems, elems_per_chunk)
    ]

    return chunks

def send_large_image(name: str, task_name: str, image: np.ndarray, chunk_size: int = 10_000_000):
    """Package an image array into chunked websocket message dictionaries.

    Args:
        name: Logical image name included in each chunk message.
        task_name: Server task identifier stored in each message.
        image: Flat or shaped NumPy array containing image data.
        chunk_size: Maximum chunk size in bytes before the tensor is split.

    Returns:
        list[dict]: Binary-ready message payloads containing tensor chunks.
    """
    chunks = split_tensor(torch.from_numpy(image), max_chunk_bytes=chunk_size)
    total_chunks = len(chunks)
    msgs = []
    for idx in range(total_chunks):
        chunk = chunks[idx]
        msgs.append({
            "type": "image",
            "name": name,
            "task_name": task_name,
            "chunk_index": idx,
            "chunk_total": total_chunks,
            "image": chunk,
        })
    return msgs  # your binary-packer function
