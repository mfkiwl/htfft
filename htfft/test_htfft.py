import os
import math
import shutil
from random import Random
import collections
import jinja2
import pytest

from numpy import fft
import cocotb
from cocotb import clock, triggers

from htfft import helper, conversions

basedir = os.path.abspath(os.path.dirname(__file__))


def get_expected_discrepancy(input_width, n):
    component_width = input_width/2
    n_increments = pow(2, component_width)
    increment_size = 2/n_increments # because we go from -1 to 1
    average_error = increment_size*0.75
    # Empirically each time N doubles, error goes up by 1.6
    expected_error = pow(1.6, math.log(n)/math.log(2)) * average_error
    return expected_error


async def send_data(rnd, dut, sent_queue, n, spcc, input_width):
    while True:
        values = [helper.random_complex(rnd, input_width)
                for i in range(n)]
        sent_queue.append(values)
        lumps = [values[index*spcc: (index+1)*spcc]
                for index in range(n//spcc)]
        dut.i_first <= 1
        for lump in lumps:
            lump_as_slv = conversions.list_of_complex_to_slv(lump, input_width)
            dut.i_data <= lump_as_slv
            await triggers.RisingEdge(dut.clk)
            dut.i_first <= 0

async def check_data(rnd, dut, sent_queue, n, spcc, input_width, output_width, n_vectors):
    assert n % spcc == 0
    n_lumps = n//spcc
    await triggers.ReadOnly()
    expected_discrepancy = get_expected_discrepancy(input_width=input_width, n=n)
    discrepancies = []
    for vector_index in range(n_vectors):
        while True:
            if str(dut.o_first.value) == '1':
                break
            await triggers.RisingEdge(dut.clk)
            await triggers.ReadOnly()
        received_data = []
        for lump_index in range(n_lumps):
            assert dut.o_first.value == (1 if lump_index == 0 else 0)
            complexes = conversions.list_of_complex_from_slv(
                dut.o_data.value.integer, output_width, spcc)
            received_data += [x * n for x in complexes]
            await triggers.RisingEdge(dut.clk)
            await triggers.ReadOnly()
        sent_data = sent_queue.popleft()
        expected_data = fft.fft(sent_data)
        assert len(received_data) == len(expected_data)
        assert len(received_data) == n
        discrepancy = pow(sum(pow(abs(a-b), 2) for a, b in zip(received_data, expected_data))/n, 0.5)
        discrepancies.append(discrepancy)
        assert discrepancy < 2*expected_discrepancy


@cocotb.test()
async def test_htfft(dut):
    test_params = helper.get_test_params()
    spcc = test_params['spcc']
    n = test_params['n']
    input_width = test_params['input_width']
    n_vectors = test_params['n_vectors']
    output_width = input_width + 2*helper.logceil(n)
    seed = test_params['seed']
    rnd = Random(seed)
    cocotb.fork(clock.Clock(dut.clk, 2, 'ns').start())
    await triggers.RisingEdge(dut.clk)
    dut.reset <= 1
    await triggers.RisingEdge(dut.clk)
    dut.reset <= 0
    sent_queue = collections.deque()
    cocotb.fork(send_data(rnd, dut, sent_queue, n, spcc, input_width,))
    await cocotb.fork(check_data(rnd, dut, sent_queue, n, spcc,
                                 input_width, output_width, n_vectors=n_vectors))


def make_htfft_core(suffix, n, spcc, input_width, twiddle_width, pipelines):
    params = {
        'suffix': suffix,
        'n': n,
        'spcc': spcc,
        'input_width': input_width,
        'twiddle_width': twiddle_width,
        'pipelines': pipelines,
        }
    template_filename = os.path.join(basedir, 'htfft.core.j2')
    with open(template_filename, 'r') as f:
        template_text = f.read()
        template = jinja2.Template(template_text)
    formatted_text = template.render(**params)
    top_filename = os.path.join(basedir, 'generated', 'htfft{}.core'.format(suffix))
    with open(top_filename, 'w') as g:
        g.write(formatted_text)


def get_test_params(n_tests, base_seed=0):
    for test_index in range(n_tests):
        seed = (base_seed + test_index) * 123214
        rnd = Random(seed)
        suffix = '_{}_test'.format(test_index)
        n = rnd.choice([8, 16, 32, 64, 128, 256])
        possible_spcc = [spcc for spcc in (2, 4, 8, 16, 32)
                         if helper.logceil(spcc) <= helper.logceil(n)/2]
        spcc = rnd.choice(possible_spcc)
        input_width = rnd.choice([8, 32])
        barrel_shifter_pipeline = ''.join(rnd.choice(('0', '1'))
                                          for i in range(helper.logceil(spcc)+1))
        generation_params = {
            'suffix': suffix,
            'n': n,
            'spcc': spcc,
            'input_width': input_width,
            'twiddle_width': input_width,
            'pipelines': {
                'barrel_shifter': barrel_shifter_pipeline,
                },
            }
        n_vectors = 10
        test_params = generation_params.copy()
        test_params['n_vectors'] = n_vectors
        test_params['seed'] = seed
        yield generation_params, test_params


@pytest.mark.parametrize(['generation_params', 'test_params'], get_test_params(n_tests=10))
def test_main(generation_params, test_params):
    suffix = generation_params['suffix']
    working_directory = os.path.abspath(os.path.join('temp', 'test_htfft_{}'.format(suffix)))
    if os.path.exists(working_directory):
        shutil.rmtree(working_directory)
    os.makedirs(working_directory)
    make_htfft_core(**generation_params)
    core_name = 'htfft' + suffix
    top_name = 'htfft' + suffix
    test_module_name = 'test_htfft'
    wave = False
    helper.run_core(working_directory, core_name, top_name, test_module_name, wave=wave,
                    test_params=test_params)


def run_tests(n_tests=10):
    working_directory = os.path.abspath('temp_test_htfft')
    if os.path.exists(working_directory):
        shutil.rmtree(working_directory)
    os.makedirs(working_directory)
    for generation_params, test_params in get_test_params(n_tests=n_tests):
        suffix = generation_params['suffix']
        make_htfft_core(**generation_params)
        core_name = 'htfft' + suffix
        top_name = 'htfft' + suffix
        test_module_name = 'test_htfft'
        wave = True
        helper.run_core(working_directory, core_name, top_name, test_module_name, wave=wave,
                        test_params=test_params)


if __name__ == '__main__':
    run_tests()
