from pathlib import Path
import onnx
from onnx import numpy_helper
import numpy as np
import onnxruntime


def _make_value_info(name):
    vi = onnx.ValueInfoProto()
    vi.name = name
    return vi


def _extract_value_info(arr, name):
    return onnx.helper.make_tensor_value_info(
        name=name,
        elem_type=onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[arr.dtype],
        shape=arr.shape,
    )



class NamedArray:
    def __init__(self, name, array):
        self.name = name
        self.array = array


class Builder:
    def __init__(
        self,
        opset_version=None,
        eval_each_node=False,
        value_prefix="onnx_builder_tmp",
    ):
        self.opset_version = opset_version
        self.eval_each_node = eval_each_node
        self.value_prefix = value_prefix
        self.__value_idx = 0
        self.__nodes = []
        self.__input_vis = []
        self.__output_vis = []
        self.__initializers = []
        self.__inputs = []
        self.__outputs = []

    def __GenValueName(self):
        self.__value_idx += 1
        return self.value_prefix + "_" + str(self.__value_idx)

    def Initializer(self, array, name=""):
        self.__inputs.append(array)
        if not name:
            name = self.__GenValueName()
        self.__input_vis.append(_extract_value_info(array, name))
        tensor = numpy_helper.from_array(array, name=name)
        self.__initializers.append(tensor)
        return NamedArray(name, array)

    def Input(self, array, name=""):
        self.__inputs.append(array)
        if not name:
            name = self.__GenValueName()
        self.__input_vis.append(_extract_value_info(array, name))
        return NamedArray(name, array)

    def Output(self, named_array, name=""):
        if name:
            for node in self.__nodes:
                if named_array.name in node.output:
                    index = list(node.output).index(named_array.name)
                    node.output[index] = name
                    break
            named_array.name = name
        self.__output_vis.append(_make_value_info(named_array.name))
        self.__outputs.append(named_array)
        return self.__outputs[-1]

    def build(self):
        graph = onnx.helper.make_graph(
            self.__nodes,
            "onnx_eval",
            inputs=self.__input_vis,
            outputs=self.__output_vis,
            initializer=self.__initializers,
        )
        opset_imports = None
        if self.opset_version is not None:
            opset_imports = [onnx.helper.make_operatorsetid("", self.opset_version)]
        model = onnx.helper.make_model(graph, opset_imports=opset_imports)
        model = onnx.shape_inference.infer_shapes(model)
        return model

    def eval(self):
        model = self.build ()
        input_names = [vi.name for vi in self.__input_vis]
        session = onnxruntime.InferenceSession(model.SerializeToString())
        inputs = dict(zip(input_names, self.__inputs))
        output_names = [vi.name for vi in self.__output_vis]
        outputs = session.run(output_names, inputs)
        for name, array in zip(output_names, outputs):
            for holder in self.__outputs:
                if holder.name == name:
                    holder.array = array
        return (model, outputs)

    def export(self, output_dir):
        model, outputs = self.eval()
        output_dir = Path(output_dir)
        (output_dir / "test_data_set_0").mkdir(parents=True, exist_ok=True)
        # save inputs
        initializer_names = [x.name for x in self.__initializers]
        input_names = [vi.name for vi in self.__input_vis]
        for i, input_ in enumerate(self.__inputs):
            if input_names[i] in initializer_names:
                continue
            tmp_pb = numpy_helper.from_array(input_, name=input_names[i])
            with open(
                output_dir / "test_data_set_0" / "input_{}.pb".format(i), "wb"
            ) as f:
                f.write(tmp_pb.SerializeToString())
        # save outputs
        output_names = [vi.name for vi in self.__output_vis]
        for i, output_ in enumerate(outputs):
            tmp_pb = numpy_helper.from_array(output_, name=output_names[i])
            with open(
                output_dir / "test_data_set_0" / "output_{}.pb".format(i), "wb"
            ) as f:
                f.write(tmp_pb.SerializeToString())
            self.__output_vis.append(_extract_value_info(output_, output_names[i]))
        onnx.save(model, output_dir / "model.onnx")

    def __getattr__(self, op):
        def fn(*args, outs=1, name=None, **kwargs):
            inputs = list(args)
            input_names = []
            for i, input_ in enumerate(inputs):
                if type(input_) == NamedArray:
                    input_names.append(input_.name)
                    inputs[i] = input_.array
                elif input_ is None:
                    input_names.append("")
                else:
                    input_names.append(self.__GenValueName())
                    constant_tensor = numpy_helper.from_array(
                        input_, name=input_names[-1] + "_val"
                    )
                    self.__nodes.append(
                        onnx.helper.make_node(
                            "Constant",
                            inputs=[],
                            outputs=[input_names[-1]],
                            value=constant_tensor,
                        )
                    )

            output_names = [self.__GenValueName() for i in range(outs)]
            for k, v in kwargs.items():
                if type(v) == np.ndarray:
                    kwargs[k] = onnx.numpy_helper.from_array(v, self.__GenValueName())
            node = onnx.helper.make_node(
                op, inputs=input_names, outputs=output_names, name=name, **kwargs
            )
            self.__nodes.append(node)

            if not self.eval_each_node:
                outputs = [NamedArray(name, None) for name in output_names]
                if outs == 1:
                    return outputs[0]
                else:
                    return outputs

            input_vis = [_extract_value_info(a, n) for n, a in zip(input_names, inputs)]
            output_vis = [_make_value_info(n) for n in output_names]
            graph = onnx.helper.make_graph(
                [node],
                "onnx_eval",
                inputs=input_vis,
                outputs=output_vis,
                initializer=self.__initializers,
            )

            opset_imports = None
            if self.opset_version is not None:
                opset_imports = [onnx.helper.make_operatorsetid("", self.opset_version)]
            model = onnx.helper.make_model(graph, opset_imports=opset_imports)
            serialized = model.SerializeToString()

            session = onnxruntime.InferenceSession(serialized)
            inputs = dict(zip(input_names, inputs))
            outputs = session.run(output_names, inputs)
            for i, output_ in enumerate(outputs):
                outputs[i] = NamedArray(output_names[i], output_)
            if outs == 1:
                return outputs[0]
            else:
                return outputs

        return fn
